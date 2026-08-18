"""Складской лист считает рулон МЕТРАМИ (аудит 2026-08-18, п. 11).

Раньше METER-строки складывались в «площадь» как есть (1 м → 1.000 кв.м), а
рулон с размером листа в карточке (1.2×2) считался листами: «продано 1 м» →
«0.42 листа», поступление — в листах, остаток — в кв.м. Владелец рулон меряет
метрами: поступление, продажи, остаток и колонки по дням — в погонных метрах,
`counted_in = "METER"`, площадь — справочно, по ширине партии.
"""
from datetime import date
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.sale_service import create_sale
from sales.models import Receipt
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class SheetRollUnitsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="sru_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        # Рулон с размером листа в карточке — так он и считался «листами».
        self.roll_mat = Material.objects.create(
            name="Акрил 8 мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"),
            sheet_width=Decimal("1.2"), sheet_height=Decimal("2"),
            price_per_pm=Decimal("1000"),
        )
        self.roll = receive_lot(
            self.roll_mat, form=Roll.Form.ROLL, width=Decimal("1.2"), length=Decimal("10"),
            purchase_cost=Decimal("4000"),
        )
        self.roll_mat.refresh_from_db()
        create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.roll_mat, "quantity": Decimal("1.5"),
                         "mode": "METER", "roll": self.roll}],
            pay_full=True,
        )

    def _row(self):
        today = date.today()
        d = self.client.get("/api/finance/material-report/", {"year": today.year, "month": today.month}).data
        return next(r for r in d["rows"] if r["id"] == self.roll_mat.id)

    def test_roll_row_is_in_metres(self):
        row = self._row()
        self.assertEqual(row["counted_in"], "METER")
        self.assertEqual(Decimal(str(row["received_qty"])), Decimal("10.00"))   # м, не 24 кв.м и не 10 листов
        self.assertEqual(Decimal(str(row["sold_qty"])), Decimal("1.50"))        # м
        self.assertEqual(Decimal(str(row["stock"])), Decimal("8.50"))           # м
        self.assertEqual(Decimal(str(row["stock_end"])), Decimal("8.50"))       # 0 + 10 − 1.5
        # Площадь — справочно, по ширине партии: 1.5 × 1.2.
        self.assertEqual(Decimal(str(row["sold_area"])), Decimal("1.800"))
        self.assertEqual(Decimal(str(row["sold_sheets"])), Decimal("0"))
        # Колонка «поступление» по дням — тоже в метрах.
        self.assertEqual(Decimal(str(row["receipts"][0]["qty"])), Decimal("10.00"))

    def test_sheet_material_still_counted_in_sheets(self):
        sheet = Material.objects.create(
            name="Форекс", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("226"),
        )
        receive_lot(sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("3000"))
        today = date.today()
        d = self.client.get("/api/finance/material-report/", {"year": today.year, "month": today.month}).data
        row = next(r for r in d["rows"] if r["id"] == sheet.id)
        self.assertEqual(row["counted_in"], "SHEET")
        self.assertEqual(Decimal(str(row["received_qty"])), Decimal("5.00"))
