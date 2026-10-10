"""Волна 2, п. 3 (XL-03/STK-08): инвентаризация и списание — 4 знака или листами.

«6 листов» 1.22×2.44 = 17.8608 кв.м. Раньше форма слала 17.86, и последний
лист было не продать; списание принимало только 2 знака: 2.9768 → 400,
2.98 → «больше остатка», 2.97 → пыль 0.0068 кв.м.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot

CHECKOUT = "/api/sales/receipts/checkout/"


class SheetsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="sh_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def sheet(self, n, cost):
        m = Material.objects.create(
            name=f"Акрил {n}", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"), piece_price=Decimal("4500"),
        )
        receive_lot(m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal(n), purchase_cost=Decimal(cost))
        m.refresh_from_db()
        return m

    def sell_sheets(self, m, n):
        return self.client.post(CHECKOUT, {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": m.id, "quantity": n, "mode": "PIECE"}],
        }, format="json")

    def test_inventory_in_sheets_keeps_exact_area_and_last_sheet_sells(self):
        m = self.sheet(6, 19200)
        self.assertEqual(m.quantity, Decimal("17.8608"))
        r = self.client.post("/api/warehouse/materials/adjust/",
                             {"material": m.id, "counted_sheets": "6", "reason": "пересчёт"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        m.refresh_from_db()
        self.assertEqual(m.quantity, Decimal("17.8608"))
        r = self.client.post("/api/warehouse/materials/adjust/",
                             {"material": m.id, "counted_quantity": "17,8608"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        m.refresh_from_db()
        self.assertEqual(m.quantity, Decimal("17.8608"))
        self.assertEqual(self.sell_sheets(m, 6).status_code, 201)
        m.refresh_from_db()
        self.assertEqual(m.quantity, Decimal("0"))

    def test_old_two_digit_inventory_does_not_block_the_last_sheet(self):
        m = self.sheet(6, 19200)
        r = self.client.post("/api/warehouse/materials/adjust/",
                             {"material": m.id, "counted_quantity": "17.86"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        r = self.sell_sheets(m, 6)
        self.assertEqual(r.status_code, 201, r.data)
        m.refresh_from_db()
        self.assertEqual(m.quantity, Decimal("0"))
        self.assertFalse(Roll.objects.filter(material=m, remaining_area__gt=0).exists())

    def test_inventory_needs_exactly_one_count(self):
        m = self.sheet(2, 6400)
        r = self.client.post("/api/warehouse/materials/adjust/", {"material": m.id}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.post("/api/warehouse/materials/adjust/",
                             {"material": m.id, "counted_quantity": "1", "counted_sheets": "1"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        bolts = Material.objects.create(name="Болт", unit=Material.Unit.PIECE, quantity=Decimal("5"))
        r = self.client.post("/api/warehouse/materials/adjust/",
                             {"material": bolts.id, "counted_sheets": "1"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def write_off(self, m, **kw):
        return self.client.post("/api/warehouse/materials/write-off/",
                                {"material": m.id, "reason_code": "OTHER", **kw}, format="json")

    def test_write_off_last_sheet_four_digits(self):
        m = self.sheet(1, 5000)
        r = self.write_off(m, quantity="2.9768")
        self.assertEqual(r.status_code, 200, r.data)
        m.refresh_from_db()
        self.assertEqual(m.quantity, Decimal("0"))

    def test_write_off_tail_goes_to_zero(self):
        for q in ("2.98", "2.97", "2,97"):
            m = self.sheet(1, 5000)
            r = self.write_off(m, quantity=q)
            self.assertEqual(r.status_code, 200, (q, r.data))
            m.refresh_from_db()
            self.assertEqual(m.quantity, Decimal("0"), q)
            log = InventoryLog.objects.filter(material=m, type=InventoryLog.Type.WRITE_OFF).get()
            self.assertEqual(log.quantity_changed, Decimal("-2.9768"))

    def test_write_off_in_sheets(self):
        m = self.sheet(3, 15000)
        r = self.write_off(m, sheets="2")
        self.assertEqual(r.status_code, 200, r.data)
        m.refresh_from_db()
        self.assertEqual(m.quantity, Decimal("2.9768"))
        r = self.write_off(m, sheets="1.5")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.write_off(m, quantity="3.5")
        self.assertEqual(r.status_code, 400, r.data)

    def test_waste_last_sheet_tail(self):
        m = self.sheet(1, 5000)
        r = self.client.post("/api/warehouse/waste/", {
            "lines": [{"material": m.id, "form": "AREA", "area": "2.97"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        m.refresh_from_db()
        self.assertEqual(m.quantity, Decimal("0"))
