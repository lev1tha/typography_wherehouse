"""Перепроверка владельца 10.10: ограды кассы и выдача по позициям.

- S2 «чек на 1,66 млрд» (CALC-06): «строка выше порога» и «деталь больше
  листа» подтверждает только админ, складовщик получает 403;
- S2 «выдача по позициям» (RU-N3): у строк с деталями выдача в деталях;
- S3 (RP-N8): неизвестные поля заказа — 400; буквы и услуги за штуку — только
  целым количеством.
"""
from decimal import Decimal as D

from sales.models import TransactionItem
from sales.tests_calc_base import RECEIPTS, CalcBase


def eng(svc, w, length, **kw):
    item = {"type": "SERVICE", "service": svc.id, "width": str(w), "length": str(length)}
    item.update(kw)
    return item


class AdminOnlyConfirmationTests(CalcBase):
    def test_storekeeper_cannot_confirm_a_billion_line(self):
        # 450 × 1230 «м» — сантиметры вместо метров: 1,66 млрд.
        r = self.co([eng(self.engr, 450, 1230)], user=self.store, confirmed_warnings=["line_total_high"])
        self.assertEqual(r.status_code, 403, r.data)
        self.assertIn("администратор", r.data["detail"])
        self.assertTrue(r.data.get("needs_admin"))
        self.assertEqual(TransactionItem.objects.count(), 0)

    def test_storekeeper_without_confirmation_is_told_to_call_the_admin(self):
        r = self.co([eng(self.engr, 450, 1230)], user=self.store)
        self.assertEqual(r.status_code, 403, r.data)
        self.assertIn("администратор", r.data["detail"])

    def test_storekeeper_cannot_confirm_a_part_bigger_than_the_sheet(self):
        r = self.co([self.cut(self.acr3, "3", "3", "12")], user=self.store,
                    confirmed_warnings=["size_exceeds_sheet"])
        self.assertEqual(r.status_code, 403, r.data)

    def test_admin_still_confirms(self):
        first = self.co([eng(self.engr, 450, 1230)])
        self.assertEqual(first.status_code, 409, first.data)
        ok = self.co([eng(self.engr, 450, 1230)], confirmed_warnings=["line_total_high"])
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_storekeeper_preview_marks_admin_only_warnings(self):
        r = self.preview([eng(self.engr, 450, 1230)], user=self.store)
        self.assertEqual(r.status_code, 200, r.data)
        found = [w for w in r.data["confirm_warnings"] if w["code"] == "line_total_high"]
        self.assertTrue(found and found[0]["admin_only"])

    def test_storekeeper_add_items_is_refused_too(self):
        r = self.co([eng(self.engr, "0.1", "0.1")], pay_full=False, amount_paid="0", client_id=self.ivan.id)
        self.client.force_authenticate(self.store)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/add-items/",
                               {"items": [eng(self.engr, 450, 1230)],
                                "confirmed_warnings": ["line_total_high"]}, format="json")
        self.assertEqual(out.status_code, 403, out.data)


class IssueByPartsTests(CalcBase):
    def setUp(self):
        super().setUp()
        r = self.co([self.cut(self.forex3, "0.2", "0.3", "1.0", parts_count=12)])
        self.assertEqual(r.status_code, 201, r.data)
        self.rid = r.data["id"]
        self.work_id, self.mat_id = [i["id"] for i in r.data["items"]]

    def issue(self, entries, user=None):
        self.client.force_authenticate(user or self.store)
        return self.client.post(f"{RECEIPTS}{self.rid}/issue/", {"items": entries}, format="json")

    def test_parts_are_issued_in_pieces(self):
        out = self.issue([{"id": self.work_id, "parts": 5}, {"id": self.mat_id, "parts": 5}])
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(out.data["fulfillment_status"], "PARTIALLY_ISSUED")
        line = next(i for i in out.data["items"] if i["id"] == self.mat_id)
        self.assertEqual(line["issued_parts"], 5)
        self.assertEqual(D(str(line["issued_qty"])), D("0.300"))       # 5 × 0,2 × 0,3
        out = self.issue([{"id": self.work_id, "parts": 7}, {"id": self.mat_id, "parts": 7}])
        self.assertEqual(out.data["fulfillment_status"], "ISSUED")
        for item in TransactionItem.objects.filter(receipt_id=self.rid):
            self.assertEqual((item.issued_parts, item.issued_qty), (12, item.quantity))

    def test_more_parts_than_left_is_refused(self):
        self.issue([{"id": self.mat_id, "parts": 10}])
        out = self.issue([{"id": self.mat_id, "parts": 3}])
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn("2", out.data["detail"])

    def test_square_metres_for_a_line_with_parts_are_refused(self):
        out = self.issue([{"id": self.mat_id, "quantity": "0.3"}])
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn("детал", out.data["detail"])

    def test_mark_issued_and_rollback_keep_parts_in_step(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f"{RECEIPTS}{self.rid}/mark-issued/", {}, format="json")
        self.assertEqual(TransactionItem.objects.get(pk=self.mat_id).issued_parts, 12)
        self.client.post(f"{RECEIPTS}{self.rid}/mark-ready/", {}, format="json")
        self.assertEqual(TransactionItem.objects.get(pk=self.mat_id).issued_parts, 0)


class CheckoutInputTests(CalcBase):
    def test_unknown_order_fields_are_refused(self):
        r = self.co([eng(self.engr, "0.1", "0.1")], is_urgnet=True)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("is_urgnet", r.data)
        pv = self.preview([eng(self.engr, "0.1", "0.1")], foo="bar")
        self.assertEqual(pv.status_code, 400, pv.data)

    def test_letters_only_by_whole_pieces(self):
        r = self.co([{"type": "SERVICE", "service": self.letters.id, "quantity": "2.5"}])
        self.assertEqual(r.status_code, 400, r.data)
        ok = self.co([{"type": "SERVICE", "service": self.letters.id, "quantity": "3"}])
        self.assertEqual(ok.status_code, 201, ok.data)
        self.client.force_authenticate(self.admin)
        line = ok.data["items"][0]["id"]
        out = self.client.post(f"{RECEIPTS}{ok.data['id']}/edit-items/",
                               {"items": [{"id": line, "quantity": "2.5"}]}, format="json")
        self.assertEqual(out.status_code, 400, out.data)
