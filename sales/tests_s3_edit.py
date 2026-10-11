"""S3 перепроверки владельца: окно правки чека.

- оценка итога до сохранения — тем же расчётом, что сохранение (предпросмотр
  правки `edit-items` с `dry_run`): 12 → 10 деталей — 1 125 и в окне, и после;
- длина реза строки с деталями и размерами не правится «количеством» — только
  детали, размеры или пог.м на деталь (`running_meters`), иначе детали и
  материал куска расходятся.
"""
from decimal import Decimal as D

from audit.models import AuditLog
from sales.models import Receipt, TransactionItem
from sales.tests_calc_base import RECEIPTS, CalcBase
from warehouse.models import Material


class EditPreviewTests(CalcBase):
    def setUp(self):
        super().setUp()
        # Акрил 3 мм 0,2 × 0,3, рез 0,3 пог.м на деталь, 12 деталей, ставка материала 65:
        # работа 3,6 × 65 = 234, материал 0,72 × 1 550 = 1 116 → 1 350.
        r = self.co([self.cut(self.acr3, "0.2", "0.3", "0.3", parts_count=12)],
                    client_id=self.ivan.id, pay_full=False, amount_paid="0")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("1350"))
        self.rid = r.data["id"]
        self.work_id, self.mat_id = [i["id"] for i in r.data["items"]]
        self.stock0 = Material.objects.get(pk=self.acr3.pk).quantity

    def edit(self, changes, **extra):
        self.client.force_authenticate(self.admin)
        return self.client.post(f"{RECEIPTS}{self.rid}/edit-items/", {"items": changes, **extra}, format="json")

    def line(self, data, pk):
        return next(i for i in data["items"] if i["id"] == pk)

    def test_preview_of_twelve_to_ten_parts_matches_the_save(self):
        logs = AuditLog.objects.count()
        pv = self.edit([{"id": self.work_id, "parts_count": 10}], dry_run=True)
        self.assertEqual(pv.status_code, 200, pv.data)
        self.assertTrue(pv.data["dry_run"])
        self.assertEqual(D(str(pv.data["total_price"])), D("1125"))          # 195 + 930
        self.assertEqual(D(str(self.line(pv.data, self.work_id)["quantity"])), D("3.000"))
        self.assertEqual(D(str(self.line(pv.data, self.mat_id)["quantity"])), D("0.600"))
        # Ничего не сохранилось: ни чек, ни склад, ни журнал.
        self.assertEqual(Receipt.objects.get(pk=self.rid).total_price, D("1350"))
        self.assertEqual(TransactionItem.objects.get(pk=self.work_id).parts_count, 12)
        self.assertEqual(Material.objects.get(pk=self.acr3.pk).quantity, self.stock0)
        self.assertEqual(AuditLog.objects.count(), logs)

        saved = self.edit([{"id": self.work_id, "parts_count": 10}])
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(D(str(saved.data["total_price"])), D(str(pv.data["total_price"])))

    def test_preview_refuses_like_the_save(self):
        out = self.edit([{"id": self.work_id, "parts_count": 0}], dry_run=True)
        self.assertEqual(out.status_code, 400, out.data)

    def test_cut_metres_with_parts_and_sizes_are_not_edited_by_quantity(self):
        out = self.edit([{"id": self.work_id, "quantity": "3"}])
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn("детали", out.data["detail"])
        self.assertEqual(TransactionItem.objects.get(pk=self.work_id).quantity, D("3.600"))

    def test_running_metres_per_part_are_editable(self):
        out = self.edit([{"id": self.work_id, "running_meters": "0.4"}])
        self.assertEqual(out.status_code, 200, out.data)
        work = TransactionItem.objects.get(pk=self.work_id)
        self.assertEqual((work.quantity, work.parts_count), (D("4.800"), 12))      # 12 × 0,4
        self.assertEqual(TransactionItem.objects.get(pk=self.mat_id).quantity, D("0.720"))
        self.assertEqual(D(str(out.data["total_price"])), D("1428"))               # 312 + 1 116

    def test_running_metres_follow_new_parts(self):
        out = self.edit([{"id": self.work_id, "running_meters": "0.5", "parts_count": 10}])
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(TransactionItem.objects.get(pk=self.work_id).quantity, D("5.000"))

    def test_running_metres_only_on_a_cut(self):
        out = self.edit([{"id": self.mat_id, "running_meters": "0.5"}])
        self.assertEqual(out.status_code, 400, out.data)

    def test_whole_sheet_cut_still_edits_its_metres(self):
        r = self.co([{"type": "SERVICE", "service": self.cnc.id, "material": self.forex3.id,
                      "running_meters": "12"}], pay_full=False, amount_paid="0", buyer_name="x")
        self.assertEqual(r.status_code, 201, r.data)
        work = r.data["items"][0]
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/edit-items/",
                               {"items": [{"id": work["id"], "quantity": "10"}]}, format="json")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(TransactionItem.objects.get(pk=work["id"]).quantity, D("10.000"))
