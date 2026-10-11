"""S3 перепроверки владельца 10.10 — склад: наценка листа, «К заказу», тыйын
последнего листа (STK-03, STK-06, STK-10).

Сценарии — `stkre.py` владельца (test_points): акрил 3 мм, партия 10 листов за
34 800, наценка 50 %, минимум 12 листов; акрил 10 мм, 7 листов за 10 000.
"""
from datetime import date
from decimal import Decimal

from finance.reports.bridge import bridge
from warehouse.models import InventoryLog, Material, Roll, stock_value_total
from warehouse.reorder import low_stock_text, reorder_rows
from warehouse.rolls import consume_area, receive_lot
from warehouse.tests_recheck_stock import OCT, Base, noon

D = Decimal
CHECKOUT = "/api/sales/receipts/checkout/"


class SheetMarkupTests(Base):
    """STK-03: подсказка и маржа листового — от закупа ЛИСТА."""

    def setUp(self):
        super().setUp()
        receive_lot(self.m, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"),
                    sheet_count=D("10"), purchase_cost=D("34800"), received_at=noon(date(2026, 9, 2)),
                    user=self.admin)
        self.m.refresh_from_db()
        self.m.markup_percent = D("50")
        self.m.save()

    def pricing(self):
        return self.client.get(f"/api/warehouse/materials/{self.m.id}/").data["pricing"]

    def test_suggested_sheet_price_from_sheet_cost(self):
        p = self.pricing()
        self.assertEqual(D(str(p["cost"]["sheet"])), D("3480.00"))
        # Excel: =ОКРВВЕРХ(3480*1,5;1) = 5 220 (было 5 221 — от 1 169,0406… × 2,9768).
        self.assertEqual(D(str(p["suggested"]["sheet"])), D("5220"))

    def test_margin_of_a_sheet_material_is_the_sheet_margin(self):
        p = self.pricing()
        self.assertEqual(p["unit"], "sheet")
        # (7 000 − 3 480) / 7 000 = 50,3 % (было 53,2 % — маржа цены кв.м).
        self.assertEqual(D(str(p["margin_percent"])), D("50.3"))

    def test_sheet_without_piece_price_keeps_the_sqm_margin(self):
        self.m.piece_price = D("0")
        self.m.save()
        p = self.pricing()
        self.assertEqual(p["unit"], "sqm")
        self.assertEqual(D(str(p["margin_percent"])), D("53.2"))


class ReorderToTests(Base):
    """STK-06: «заказывать до» у материала; по умолчанию — 2 × минимума."""

    def setUp(self):
        super().setUp()
        receive_lot(self.m, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"),
                    sheet_count=D("10"), purchase_cost=D("34800"), received_at=noon(date(2026, 9, 2)),
                    user=self.admin)
        self.m.refresh_from_db()
        self.m.min_stock = D("12")
        self.m.save()

    def row(self):
        return next(r for r in reorder_rows() if r["id"] == self.m.id)

    def test_default_is_up_to_twice_the_minimum_and_says_so(self):
        row = self.row()
        self.assertEqual(row["to_order"], D("14"))
        self.assertEqual(row["target"], D("24"))
        self.assertEqual(row["target_rule"], "double_min")

    def test_order_up_to_from_the_card(self):
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {"reorder_to": "12"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["reorder_to"])), D("12.00"))
        row = self.row()
        # Excel владельца: =МАКС(0; 12 − 10) = 2 листа.
        self.assertEqual(row["to_order"], D("2"))
        self.assertEqual(row["target"], D("12"))
        self.assertEqual(row["target_rule"], "field")
        self.assertIn("Заказать ≈2 лист.", low_stock_text(Material.objects.get(pk=self.m.pk)))
        api = self.client.get("/api/warehouse/materials/reorder/").data
        rows = api if isinstance(api, list) else api.get("rows", api.get("results"))
        self.assertEqual(D(str(next(x for x in rows if x["id"] == self.m.id)["to_order"])), D("2"))

    def test_negative_order_up_to_is_refused(self):
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {"reorder_to": "-1"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("reorder_to", r.data)

    def test_target_below_stock_orders_nothing(self):
        self.m.reorder_to = D("5")
        self.m.save()
        self.assertEqual(self.row()["to_order"], D("0"))


class LastSheetTakesTheRestTests(Base):
    """STK-10: 7 листов по одному из партии 10 000 — себестоимость ровно 10 000,00."""

    def setUp(self):
        super().setUp()
        self.m2 = Material.objects.create(
            name="акрил 10 мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET, sheet_width=D("1.22"), sheet_height=D("2.44"),
            price_per_sqm=D("2000"), piece_price=D("6000"),
        )
        receive_lot(self.m2, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"), sheet_count=D("7"),
                    purchase_cost=D("10000"), received_at=noon(date(2026, 10, 3)), user=self.admin,
                    paid_account="CASH")

    def test_seven_sheets_sold_one_by_one_cost_the_whole_lot(self):
        costs = []
        for _ in range(7):
            r = self.client.post(CHECKOUT, {
                "payment_method": "CASH", "pay_full": True, "order_date": "2026-10-05",
                "items": [{"type": "MATERIAL", "material": self.m2.id, "mode": "PIECE", "quantity": "1"}],
            }, format="json")
            self.assertEqual(r.status_code, 201, r.data)
            costs.append(D(str(r.data["cost_total"])))
        self.assertEqual(sum(costs), D("10000.00"), costs)
        self.assertTrue(all(c in (D("1428.57"), D("1428.58")) for c in costs), costs)
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.stock_value, D("0.00"))
        sold = sum((e.cost for e in InventoryLog.objects.filter(material=self.m2, type=InventoryLog.Type.SALE)), D("0"))
        self.assertEqual(sold, D("10000.00"))
        self.assertEqual(bridge(*OCT)["unexplained"], D("0"))

    def test_shelf_value_drops_exactly_by_each_sale(self):
        """Стоимость полки после каждой продажи — ровно закуп минус проданное."""
        sheet = self.m2.piece_area
        spent = D("0")
        for _ in range(3):
            spent += consume_area(self.m2, sheet, log_type=InventoryLog.Type.SALE)
            self.m2.refresh_from_db()
            self.assertEqual(self.m2.stock_value + spent, D("10000.00"))
        self.assertEqual(stock_value_total(), D("10000.00") - spent)

    def test_whole_lot_at_once_is_still_the_purchase(self):
        cost = consume_area(self.m2, self.m2.piece_area * 7, log_type=InventoryLog.Type.SALE)
        self.assertEqual(cost, D("10000.00"))
