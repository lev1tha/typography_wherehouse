"""Полка остатков (2026-10-11, просьба владельца ЧПУ-центра).

После заказа остаются мелкие годные детали и куски — брак, лишние детали,
обрезки. По учёту они уже «проданы»: материал ушёл в себестоимость заказа (в
том числе с КИМ раскроя, D-127), а физически лежат на полке, и их продают
немного дешевле. Полка — учёт этих кусков: что лежит, от какого заказа и
материала, сколько продано и что выбросили.

Главное правило: себестоимость остатка — НОЛЬ. В стоимость склада, остаток
материала, партии, ОПиУ и сверку он не входит: это уже списанный материал.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from sales.models import Receipt
from sales.sale_service import create_sale
from warehouse.models import InventoryLog, Leftover, Material, ProductionSite, Roll, stock_value_total
from warehouse.rolls import receive_lot

URL = "/api/warehouse/leftovers/"


class LeftoverBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="lo_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="lo_store", password="x", role=User.Role.STOREKEEPER)
        self.acc = User.objects.create_user(username="lo_acc", password="x", role=User.Role.ACCOUNTANT)
        self.acryl = Material.objects.create(
            name="Акрил 3 мм", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"),
        )
        self.film = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("500"),
        )
        receive_lot(self.acryl, form="ROLL", width=Decimal("1"), length=Decimal("10"),
                    purchase_cost=Decimal("2000"))
        self.order = create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.acryl, "quantity": Decimal("2"), "mode": "SQM"}],
            pay_full=True,
        )

    def _put(self, user=None, **over):
        self.client.force_authenticate(user or self.store)
        body = {
            "material": self.acryl.id, "measure": "SQM", "width": "0.3", "length": "0.4",
            "pieces": 3, "source_receipt": str(self.order.id), "note": "лишние детали",
        }
        body.update(over)
        return self.client.post(URL, body, format="json")


class PutOnShelfTests(LeftoverBase):
    def test_storekeeper_puts_pieces_from_an_order(self):
        r = self._put()
        self.assertEqual(r.status_code, 201, r.data)
        lo = Leftover.objects.get(pk=r.data["id"])
        self.assertEqual(lo.material, self.acryl)
        self.assertEqual(lo.pieces, 3)
        self.assertEqual(lo.pieces_left, 3)
        self.assertEqual(lo.status, Leftover.Status.ON_SHELF)
        self.assertEqual(lo.source_receipt_id, self.order.id)
        self.assertEqual(lo.created_by, self.store)
        self.assertEqual(r.data["source_receipt_number"], self.order.order_number)
        # Площадь одного куска 0.12, на полке 3 куска — 0.36 кв.м.
        self.assertEqual(Decimal(str(r.data["area_left"])), Decimal("0.36"))
        self.assertEqual(r.data["created_by_name"], "lo_store")

    def test_admin_puts_by_hand_without_order(self):
        site = ProductionSite.objects.create(code="glob", name="Глобал")
        r = self._put(self.admin, source_receipt=None, measure="METER", width=None,
                      length="1.5", pieces=1, material=self.film.id, site=site.id, note="")
        self.assertEqual(r.status_code, 201, r.data)
        lo = Leftover.objects.get(pk=r.data["id"])
        self.assertIsNone(lo.source_receipt_id)
        self.assertEqual(lo.site, site)
        self.assertEqual(Decimal(str(r.data["metres_left"])), Decimal("1.5"))

    def test_pieces_measure_needs_no_size(self):
        r = self._put(measure="PIECE", width=None, length=None, pieces=12, note="уголки")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["pieces_left"], 12)

    def test_several_at_once_from_the_order_card(self):
        self.client.force_authenticate(self.store)
        r = self.client.post(URL, {"items": [
            {"material": self.acryl.id, "measure": "SQM", "width": "0.3", "length": "0.4", "pieces": 2,
             "source_receipt": str(self.order.id)},
            {"material": self.acryl.id, "measure": "PIECE", "pieces": 5, "source_receipt": str(self.order.id)},
        ]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(r.data["results"]), 2)
        self.assertEqual(Leftover.objects.filter(source_receipt=self.order).count(), 2)

    def test_bad_input_is_refused(self):
        self.assertEqual(self._put(length=None).status_code, 400)          # кв.м без длины
        self.assertEqual(self._put(width="0").status_code, 400)            # нулевая ширина
        self.assertEqual(self._put(pieces=0).status_code, 400)             # ноль кусков
        self.assertEqual(self._put(measure="METER", width="0.3").status_code, 400)  # у пог.м ширины нет
        self.assertEqual(self._put(material=None).status_code, 400)        # материал обязателен
        self.assertFalse(Leftover.objects.exists())

    def test_accountant_only_looks(self):
        self._put()
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.client.get(URL).status_code, 200)
        r = self.client.post(URL, {"material": self.acryl.id, "measure": "PIECE", "pieces": 1}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_journal_shows_before_and_after(self):
        self._put()
        rec = AuditLog.objects.filter(action__startswith="Полка").latest("id")
        self.assertIn("Акрил 3 мм", rec.action)
        self.assertIn("было 0 → стало 3", rec.action)
        self.assertEqual(rec.kind, "stock")


class ZeroCostTests(LeftoverBase):
    """Остаток уже списан заказом — склад, партии и сверка его не видят."""

    def test_stock_value_and_material_do_not_change(self):
        value = stock_value_total()
        qty = Material.objects.get(pk=self.acryl.pk).quantity
        lots = list(Roll.objects.filter(material=self.acryl).values_list("id", "remaining_area"))
        logs = InventoryLog.objects.count()
        self._put()
        self._put(measure="PIECE", width=None, length=None, pieces=40)
        self.assertEqual(stock_value_total(), value)
        self.assertEqual(Material.objects.get(pk=self.acryl.pk).quantity, qty)
        self.assertEqual(
            list(Roll.objects.filter(material=self.acryl).values_list("id", "remaining_area")), lots,
        )
        self.assertEqual(InventoryLog.objects.count(), logs)

    def test_finance_report_reconciles_to_zero(self):
        self.client.force_authenticate(self.admin)
        before = self.client.get("/api/finance/report/").data["stock"]
        self._put()
        self.client.force_authenticate(self.admin)
        after = self.client.get("/api/finance/report/").data["stock"]
        self.assertEqual(Decimal(str(after["reconcile"]["gap"])), Decimal("0"))
        self.assertEqual(after["reconcile"]["value_now"], before["reconcile"]["value_now"])
        self.assertEqual(after["reconcile"]["expected"], before["reconcile"]["expected"])

    def test_material_card_has_no_shelf_quantity(self):
        self._put()
        self.client.force_authenticate(self.admin)
        r = self.client.get(f"/api/warehouse/materials/{self.acryl.id}/")
        self.assertEqual(Decimal(str(r.data["quantity"])), Material.objects.get(pk=self.acryl.pk).quantity)


class ShelfScreenTests(LeftoverBase):
    def _ids(self, **params):
        self.client.force_authenticate(self.store)
        r = self.client.get(URL, params)
        self.assertEqual(r.status_code, 200, r.data)
        return [row["id"] for row in r.data["results"]]

    def test_filters_material_measure_status_age_size(self):
        a = self._put().data["id"]                                           # 0.3×0.4, 3 шт
        b = self._put(width="1.2", length="0.5", pieces=1).data["id"]         # 1.2×0.5
        c = self._put(material=self.film.id, measure="METER", width=None, length="2", pieces=1).data["id"]
        Leftover.objects.filter(pk=a).update(created_at=timezone.now() - timedelta(days=100))
        self.assertEqual(self._ids(material=self.film.id), [c])
        self.assertEqual(self._ids(measure="METER"), [c])
        self.assertEqual(self._ids(age_min=90), [a])
        # Самые старые — сверху.
        self.assertEqual(self._ids()[0], a)
        # Поиск по размеру: подойдёт ли кусок 0.45 × 1.0 — в любом повороте.
        self.assertEqual(self._ids(min_width="0.45", min_length="1.0", measure="SQM"), [b])
        self.assertEqual(self._ids(min_width="1.0", min_length="0.45", measure="SQM"), [b])
        self.assertEqual(self._ids(q="оракал"), [c])
        Leftover.objects.filter(pk=b).update(status=Leftover.Status.WRITTEN_OFF, pieces_left=0)
        self.assertEqual(self._ids(status="WRITTEN_OFF"), [b])
        # По умолчанию — только то, что лежит на полке.
        self.assertNotIn(b, self._ids())
        self.assertIn(b, self._ids(status="ALL"))

    def test_summary_per_material_oldest_first(self):
        a = self._put().data["id"]                                           # 3 × 0.12 = 0.36
        self._put(width="1", length="0.5", pieces=2)                          # 2 × 0.5 = 1.0
        self._put(material=self.film.id, measure="METER", width=None, length="2", pieces=2)
        Leftover.objects.filter(pk=a).update(created_at=timezone.now() - timedelta(days=30))
        self.client.force_authenticate(self.acc)
        r = self.client.get(URL + "summary/")
        self.assertEqual(r.status_code, 200, r.data)
        rows = r.data["rows"]
        self.assertEqual(rows[0]["material_name"], "Акрил 3 мм")
        self.assertEqual(rows[0]["pieces"], 5)
        self.assertEqual(Decimal(str(rows[0]["area"])), Decimal("1.36"))
        self.assertGreaterEqual(rows[0]["oldest_days"], 30)
        self.assertEqual(rows[1]["material_name"], "Оракал")
        self.assertEqual(Decimal(str(rows[1]["metres"])), Decimal("4"))
        self.assertEqual(r.data["totals"]["pieces"], 7)

    def test_write_off_is_admin_only_with_reason(self):
        lo_id = self._put().data["id"]
        url = f"{URL}{lo_id}/write-off/"
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.post(url, {"reason": "выбросили"}, format="json").status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(url, {"reason": " "}, format="json").status_code, 400)
        r = self.client.post(url, {"reason": "треснул, выбросили"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        lo = Leftover.objects.get(pk=lo_id)
        self.assertEqual(lo.status, Leftover.Status.WRITTEN_OFF)
        self.assertEqual(lo.pieces_left, 0)
        self.assertEqual(lo.written_off_pieces, 3)
        self.assertEqual(lo.write_off_reason, "треснул, выбросили")
        self.assertEqual(lo.written_off_by, self.admin)
        rec = AuditLog.objects.filter(action__startswith="Полка").latest("id")
        self.assertIn("было 3 → стало 0", rec.action)
        self.assertIn("треснул", rec.action)
        # Второй раз списывать нечего.
        self.assertEqual(self.client.post(url, {"reason": "ещё"}, format="json").status_code, 400)
        # Списание — тоже не склад: стоимость и журнал склада не двигались.
        self.assertFalse(InventoryLog.objects.filter(type=InventoryLog.Type.WRITE_OFF).exists())

    def test_csv_for_excel(self):
        self._put()
        self.client.force_authenticate(self.store)
        r = self.client.get(URL + "export/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        body = r.content.decode("utf-8")
        self.assertTrue(body.startswith("﻿"))
        header, first = body.lstrip("﻿").splitlines()[:2]
        self.assertIn(";", header)
        self.assertIn("Материал", header)
        self.assertIn("Акрил 3 мм", first)
        self.assertIn("0,3", first)       # размер с запятой
        self.assertIn("0,36", first)      # площадь на полке

    def test_receipt_card_offers_order_materials(self):
        """«Остатки на полку» из карточки: материал по умолчанию — из строк заказа."""
        self.client.force_authenticate(self.store)
        r = self.client.get(URL + "from-receipt/", {"receipt": str(self.order.id)})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([m["id"] for m in r.data["materials"]], [self.acryl.id])
        self.assertEqual(r.data["order_number"], self.order.order_number)

    def test_deleting_the_source_order_keeps_the_pieces(self):
        """Остаток ссылается на заказ, но живёт сам по себе: заказ можно удалить."""
        lo_id = self._put().data["id"]
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f"/api/sales/receipts/{self.order.id}/").status_code, 204)
        self.assertFalse(Receipt.objects.filter(pk=self.order.pk).exists())
        lo = Leftover.objects.get(pk=lo_id)
        self.assertIsNone(lo.source_receipt_id)
        self.assertEqual(lo.pieces_left, 3)
