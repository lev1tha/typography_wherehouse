"""Продажа с полки остатков (2026-10-11, просьба владельца).

Касса → «Отходы» → «С полки»: выбрали кусок, сколько кусков, цену — ВРУЧНУЮ
(каталога у остатка нет). Строка — та же услуга «Отходы» (выручка вида
«Отходы», себестоимость 0, склад не двигается), но со ссылкой на остаток:
полка уменьшается, возврат, удаление и отмена кладут куски обратно, продать
один кусок дважды нельзя.
"""
from datetime import date
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from finance.reports import bridge as bridge_mod
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale
from services.models import PricingSettings, PrintingService
from warehouse.models import InventoryLog, Leftover, Material, stock_value_total
from warehouse.rolls import receive_lot

CHECKOUT = "/api/sales/receipts/checkout/"


class ShelfSaleBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ls_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="ls_store", password="x", role=User.Role.STOREKEEPER)
        self.waste = PrintingService.objects.get(kind=PrintingService.Kind.WASTE)
        # Каталог отходов есть, но к остаткам с полки он не относится.
        self.waste.rate_flat = Decimal("300")
        self.waste.rate_per_pm = Decimal("120")
        self.waste.rate_per_piece = Decimal("1000")
        self.waste.save()
        self.acryl = Material.objects.create(
            name="Акрил 3 мм", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"),
        )
        receive_lot(self.acryl, form="ROLL", width=Decimal("1"), length=Decimal("10"),
                    purchase_cost=Decimal("2000"))
        self.order = create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.acryl, "quantity": Decimal("2"), "mode": "SQM"}],
            pay_full=True,
        )
        self.lo = Leftover.objects.create(
            material=self.acryl, measure=Leftover.Measure.SQM, width=Decimal("0.3"),
            length=Decimal("0.4"), pieces=3, pieces_left=3, source_receipt=self.order,
            created_by=self.store,
        )

    def _line(self, **over):
        line = {"type": "SERVICE", "service": self.waste.id, "leftover": self.lo.id,
                "quantity": 2, "cut_rate": "300"}
        line.update(over)
        return {k: v for k, v in line.items() if v is not None}

    def _sell(self, user=None, lines=None, **extra):
        self.client.force_authenticate(user or self.store)
        return self.client.post(
            CHECKOUT, {"payment_method": "CASH", "pay_full": True, "items": lines or [self._line()], **extra},
            format="json",
        )

    def _left(self):
        self.lo.refresh_from_db()
        return self.lo.pieces_left


class SellFromShelfTests(ShelfSaleBase):
    def test_sell_two_of_three_at_own_price(self):
        value = stock_value_total()
        logs = InventoryLog.objects.count()
        r = self._sell()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("600"))
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertEqual(item.leftover, self.lo)
        self.assertEqual(item.service, self.waste)
        self.assertEqual(item.sale_mode, TransactionItem.SaleMode.PIECE)
        self.assertEqual(item.quantity, Decimal("2"))
        self.assertEqual(item.price_per_item, Decimal("300"))
        self.assertTrue(item.price_is_manual)
        self.assertEqual(item.cost_total, Decimal("0"))
        self.assertEqual(self._left(), 1)
        self.assertEqual(self.lo.status, Leftover.Status.PARTIAL)
        # Склад и себестоимость не двигаются — как у «Отходов».
        self.assertEqual(stock_value_total(), value)
        self.assertEqual(InventoryLog.objects.count(), logs)
        # В чеке видно, какой кусок продали.
        self.assertIn("Акрил 3 мм", r.data["items"][0]["leftover_label"])
        self.assertEqual(r.data["items"][0]["leftover"], self.lo.id)
        rec = AuditLog.objects.filter(action__startswith="Полка").latest("id")
        self.assertIn("было 3 → стало 1", rec.action)
        self.assertIn(f"№{r.data['order_number']}", rec.action)

    def test_selling_everything_marks_it_sold(self):
        r = self._sell(lines=[self._line(quantity=3)])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self._left(), 0)
        self.assertEqual(self.lo.status, Leftover.Status.SOLD)

    def test_price_is_required_and_positive(self):
        for user in (self.store, self.admin):
            self.assertEqual(self._sell(user, [self._line(cut_rate=None)]).status_code, 400)
            self.assertEqual(self._sell(user, [self._line(cut_rate="0")]).status_code, 400)
        self.assertEqual(self._left(), 3)
        self.assertFalse(Receipt.objects.exclude(pk=self.order.pk).exists())

    def test_catalogue_price_typed_by_hand_stays_manual(self):
        """Цена, совпавшая с каталогом отходов, — всё равно цена остатка, а не каталог."""
        r = self._sell(lines=[self._line(cut_rate="1000", quantity=1)])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(TransactionItem.objects.get(receipt_id=r.data["id"]).price_is_manual)

    def test_no_low_price_warning_for_shelf_lines(self):
        """D-182 («ниже 50 % каталога») к остаткам не относится: каталога у них нет."""
        s = PricingSettings.load()
        s.staff_price_warn_percent = Decimal("50")
        s.staff_min_price_percent = Decimal("40")
        s.save()
        r = self._sell(lines=[self._line(cut_rate="100", quantity=1)])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertFalse([w for w in r.data["warnings"] if w.get("code") == "low_manual_price"])
        self.client.force_authenticate(self.store)
        p = self.client.post("/api/sales/receipts/preview/", {
            "payment_method": "CASH", "items": [self._line(cut_rate="100", quantity=1)],
        }, format="json")
        self.assertEqual(p.status_code, 200, p.data)
        self.assertFalse([w for w in p.data["warnings"] if w.get("code") == "low_manual_price"])

    def test_typed_price_is_final(self):
        """Минимум строки и срочность к цене куска не применяются: её назвали сами."""
        s = PricingSettings.load()
        s.min_line_amount = Decimal("500")
        s.urgency_percent = Decimal("20")
        s.save()
        r = self._sell(lines=[self._line(quantity=1)], is_urgent=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("300"))

    def test_more_than_lies_is_refused(self):
        r = self._sell(lines=[self._line(quantity=4)])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("на полке", str(r.data).lower())
        self.assertEqual(self._left(), 3)
        self.assertFalse(Receipt.objects.exclude(pk=self.order.pk).exists())

    def test_the_same_piece_cannot_be_sold_twice(self):
        self.assertEqual(self._sell(lines=[self._line(quantity=3)]).status_code, 201)
        self.assertEqual(self._sell(lines=[self._line(quantity=1)]).status_code, 400)
        # Две строки одного куска в одном чеке — тоже считаются вместе.
        self.lo.pieces = 3
        self.lo.save()
        other = Leftover.objects.create(material=self.acryl, measure="PIECE", pieces=2, pieces_left=2)
        r = self._sell(lines=[self._line(leftover=other.id, quantity=2), self._line(leftover=other.id, quantity=1)])
        self.assertEqual(r.status_code, 400, r.data)
        other.refresh_from_db()
        self.assertEqual(other.pieces_left, 2)

    def test_written_off_piece_cannot_be_sold(self):
        Leftover.objects.filter(pk=self.lo.pk).update(
            pieces_left=0, written_off_pieces=3, status=Leftover.Status.WRITTEN_OFF,
        )
        self.assertEqual(self._sell(lines=[self._line(quantity=1)]).status_code, 400)

    def test_shelf_line_is_waste_service_only(self):
        engraving = PrintingService.objects.create(name="Гравировка", kind="ENGRAVING", rate_flat=Decimal("100"))
        self.assertEqual(self._sell(lines=[self._line(service=engraving.id, width="0.1", length="0.1")]).status_code, 400)
        r = self._sell(lines=[{"type": "MATERIAL", "material": self.acryl.id, "mode": "SQM",
                               "quantity": "0.1", "leftover": self.lo.id}])
        self.assertEqual(r.status_code, 400)

    def test_pieces_only_whole_and_no_sizes(self):
        self.assertEqual(self._sell(lines=[self._line(quantity="1.5")]).status_code, 400)
        self.assertEqual(self._sell(lines=[self._line(mode="SQM")]).status_code, 400)
        self.assertEqual(self._sell(lines=[self._line(width="0.3", length="0.4")]).status_code, 400)
        self.assertEqual(self._sell(lines=[self._line(quantity=0)]).status_code, 400)
        # Мерка PIECE явно — можно.
        self.assertEqual(self._sell(lines=[self._line(mode="PIECE", quantity=1)]).status_code, 201)

    def test_plain_waste_still_works(self):
        """Обратная совместимость: «Отходы» без полки — как раньше."""
        r = self._sell(lines=[{"type": "SERVICE", "service": self.waste.id, "mode": "PIECE", "quantity": 4}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("4000"))
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertIsNone(item.leftover_id)
        self.assertEqual(self._left(), 3)

    def test_quote_does_not_take_shelf_pieces(self):
        self.client.force_authenticate(self.store)
        r = self.client.post("/api/sales/quotes/", {"items": [self._line()]}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_lock_is_postgres_safe(self):
        """FOR UPDATE с outer join Postgres не принимает (D-175): только OF self."""
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self._sell().status_code, 201)
        locks = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"]]
        if connection.vendor == "postgresql":
            shelf = [sql for sql in locks if "warehouse_leftover" in sql]
            self.assertTrue(shelf)
            for sql in shelf:
                self.assertIn('FOR UPDATE OF "warehouse_leftover"', sql)


class ShelfReturnTests(ShelfSaleBase):
    def setUp(self):
        super().setUp()
        r = self._sell()
        self.assertEqual(r.status_code, 201, r.data)
        self.receipt = Receipt.objects.get(pk=r.data["id"])
        self.item = self.receipt.items.get()
        self.assertEqual(self._left(), 1)

    def test_full_refund_cancels_and_puts_pieces_back(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/sales/receipts/{self.receipt.id}/refund/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, Receipt.Status.CANCELLED)
        self.assertEqual(self._left(), 3)
        self.assertEqual(self.lo.status, Leftover.Status.ON_SHELF)
        rec = AuditLog.objects.filter(action__startswith="Полка").latest("id")
        self.assertIn("возврат", rec.action)
        self.assertIn("было 1 → стало 3", rec.action)

    def test_partial_refund_of_one_piece(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            f"/api/sales/receipts/{self.receipt.id}/refund/",
            {"quantities": [{"id": self.item.id, "quantity": "1"}]}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._left(), 2)
        self.assertEqual(self.lo.status, Leftover.Status.PARTIAL)
        lines = self.receipt.items.order_by("id")
        self.assertEqual([(l.quantity, l.is_returned, l.leftover_id) for l in lines],
                         [(Decimal("1"), False, self.lo.id), (Decimal("1"), True, self.lo.id)])

    def test_half_a_piece_cannot_come_back(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            f"/api/sales/receipts/{self.receipt.id}/refund/",
            {"quantities": [{"id": self.item.id, "quantity": "0.5"}]}, format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(self._left(), 1)

    def test_deleting_the_receipt_puts_pieces_back(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f"/api/sales/receipts/{self.receipt.id}/").status_code, 204)
        self.assertEqual(self._left(), 3)
        self.assertEqual(self.lo.status, Leftover.Status.ON_SHELF)

    def test_undo_refund_takes_the_pieces_again(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{self.receipt.id}/refund/", {}, format="json")
        self.assertEqual(self._left(), 3)
        r = self.client.post(f"/api/sales/receipts/{self.receipt.id}/undo-refund/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._left(), 1)

    def test_undo_refund_after_the_piece_was_resold_is_refused(self):
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{self.receipt.id}/refund/", {}, format="json")
        self.assertEqual(self._sell(lines=[self._line(quantity=3)]).status_code, 201)
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/sales/receipts/{self.receipt.id}/undo-refund/", {}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(self._left(), 0)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, Receipt.Status.CANCELLED)

    def test_edit_quantity_and_remove_line(self):
        self.client.force_authenticate(self.admin)
        url = f"/api/sales/receipts/{self.receipt.id}/edit-items/"
        self.assertEqual(self.client.post(url, {"items": [{"id": self.item.id, "quantity": "4"}]},
                                          format="json").status_code, 400)
        self.assertEqual(self._left(), 1)
        self.assertEqual(self.client.post(url, {"items": [{"id": self.item.id, "price_per_item": "0"}]},
                                          format="json").status_code, 400)
        r = self.client.post(url, {"items": [{"id": self.item.id, "quantity": "3"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._left(), 0)
        r = self.client.post(url, {"items": [{"id": self.item.id, "remove": True}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._left(), 3)

    def test_write_off_rest_then_return_brings_piece_back(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/warehouse/leftovers/{self.lo.id}/write-off/", {"reason": "сколот"},
                             format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._left(), 0)
        self.assertEqual(self.lo.status, Leftover.Status.WRITTEN_OFF)
        self.client.post(
            f"/api/sales/receipts/{self.receipt.id}/refund/",
            {"quantities": [{"id": self.item.id, "quantity": "1"}]}, format="json",
        )
        self.assertEqual(self._left(), 1)
        self.assertEqual(self.lo.written_off_pieces, 1)


class ShelfReportsTests(ShelfSaleBase):
    def test_summary_shows_shelf_revenue_by_material(self):
        self.assertEqual(self._sell().status_code, 201)
        self.assertEqual(self._sell(lines=[{"type": "SERVICE", "service": self.waste.id, "mode": "PIECE",
                                            "quantity": 1, "cut_rate": "50"}]).status_code, 201)
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/finance/report/")
        self.assertEqual(r.status_code, 200)
        waste = next(g for g in r.data["services"]["rows"] if g["key"] == "waste")
        self.assertEqual(Decimal(str(waste["revenue"])), Decimal("650"))
        shelf = waste["shelf"]
        self.assertEqual([(m["name"], Decimal(str(m["revenue"])), m["pieces"]) for m in shelf["materials"]],
                         [("Акрил 3 мм", Decimal("600"), 2)])
        self.assertEqual(Decimal(str(shelf["unlinked"])), Decimal("50"))

    def test_bridge_and_golden_rules_hold(self):
        """«Не объяснено» = 0: продажа с полки — выручка без себестоимости, и только."""
        r = self._sell()
        self.client.force_authenticate(self.admin)
        receipt = Receipt.objects.get(pk=r.data["id"])
        item = receipt.items.get()
        self.client.post(f"/api/sales/receipts/{receipt.id}/refund/",
                         {"quantities": [{"id": item.id, "quantity": "1"}]}, format="json")
        today = timezone.localdate()
        b = bridge_mod.bridge(today.replace(day=1), today)
        self.assertEqual(Decimal(str(b["unexplained"])), Decimal("0"))
        stock = self.client.get("/api/finance/report/").data["stock"]
        self.assertEqual(Decimal(str(stock["reconcile"]["gap"])), Decimal("0"))

    def test_shelf_history_and_period_total(self):
        r = self._sell()
        receipt = Receipt.objects.get(pk=r.data["id"])
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{receipt.id}/refund/",
                         {"quantities": [{"id": receipt.items.get().id, "quantity": "1"}]}, format="json")
        self.client.force_authenticate(self.store)
        today = timezone.localdate().isoformat()
        h = self.client.get("/api/warehouse/leftovers/sales/", {"date_from": today, "date_to": today})
        self.assertEqual(h.status_code, 200, h.data)
        self.assertEqual(Decimal(str(h.data["totals"]["sold"])), Decimal("600"))
        self.assertEqual(Decimal(str(h.data["totals"]["returned"])), Decimal("300"))
        self.assertEqual(Decimal(str(h.data["totals"]["net"])), Decimal("300"))
        self.assertEqual(h.data["totals"]["pieces"], 1)
        row = h.data["rows"][0]
        self.assertEqual(row["order_number"], receipt.order_number)
        self.assertEqual(row["leftover"], self.lo.id)
        # Карточка остатка: что и по какому чеку ушло.
        d = self.client.get(f"/api/warehouse/leftovers/{self.lo.id}/")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(len(d.data["sales"]), 2)
        self.assertEqual(Decimal(str(d.data["sold_amount"])), Decimal("300"))
        # Чужой период — пусто.
        old = date(2020, 1, 1).isoformat()
        h = self.client.get("/api/warehouse/leftovers/sales/", {"date_from": old, "date_to": old})
        self.assertEqual(h.data["rows"], [])
