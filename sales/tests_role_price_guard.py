"""Цену и ставку при продаже правит только админ; закупочные цифры складовщику
не отдаются (аудит 2026-08-18, п. 14).

В кассе у складовщика полей цены и ставки нет, но API принимал
`material_price` / `cut_rate` от кого угодно — складовщик с консолью браузера
оформлял лист и резку за 0 при себестоимости 4 087. А список рулонов и
карточка материала отдавали `purchase_cost` / `cost_per_pm` / `purchase_price`
всем: касса складовщика подписывала рулон «№8 · 2 м · 200 сом/м» — закупкой.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class RolePriceGuardTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="rp_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="rp_store", password="x", role=User.Role.STOREKEEPER)
        self.acct = User.objects.create_user(username="rp_acct", password="x", role=User.Role.ACCOUNTANT)
        self.sheet = Material.objects.create(
            name="Белый акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1250"), piece_price=Decimal("3700"), cut_rate_per_pm=Decimal("20"),
        )
        receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("17500"))
        self.roll_mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"), price_per_pm=Decimal("1000"),
        )
        receive_lot(self.roll_mat, form=Roll.Form.ROLL, width=Decimal("1.2"), length=Decimal("10"),
                    purchase_cost=Decimal("4000"))
        self.cutting = PrintingService.objects.create(name="Резка", kind=PrintingService.Kind.CUTTING)

    def _checkout(self, items):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "pay_full": True, "items": items},
            format="json",
        )

    def test_storekeeper_cannot_override_price_or_rate(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE",
                             "material_price": 0}])
        self.assertEqual(r.status_code, 403, r.data)
        r = self._checkout([{"type": "SERVICE", "service": self.cutting.id, "material": self.sheet.id,
                             "width": "0.5", "length": "1", "running_meters": "2", "cut_rate": 0}])
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(Receipt.objects.count(), 0)
        # Без ручных цен — обычная продажа по каталогу.
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("3700"))

    def test_storekeeper_cannot_override_in_add_items(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"}])
        rid = r.data["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE",
                        "material_price": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)

    def test_admin_still_overrides(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE",
                             "material_price": 3000}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("3000"))

    def test_rolls_api_hides_cost_from_storekeeper(self):
        self.client.force_authenticate(self.store)
        rows = self.client.get("/api/warehouse/rolls/", {"material": self.roll_mat.id}).data["results"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["cost_per_pm"])
        self.assertIsNone(rows[0]["cost_per_sqm"])
        self.assertIsNone(rows[0]["purchase_cost"])
        self.assertEqual(Decimal(str(rows[0]["metres_remaining"])), Decimal("10.00"))
        # Владелец и бухгалтер видят.
        for user in (self.admin, self.acct):
            self.client.force_authenticate(user)
            rows = self.client.get("/api/warehouse/rolls/", {"material": self.roll_mat.id}).data["results"]
            self.assertEqual(Decimal(str(rows[0]["cost_per_pm"])), Decimal("400.00"))

    def test_materials_api_hides_purchase_price_and_stock_value_from_storekeeper(self):
        self.client.force_authenticate(self.store)
        row = self.client.get(f"/api/warehouse/materials/{self.sheet.id}/").data
        self.assertIsNone(row["purchase_price"])
        self.assertIsNone(row["stock_value"])
        self.assertEqual(Decimal(str(row["price_per_sqm"])), Decimal("1250"))
        self.client.force_authenticate(self.admin)
        row = self.client.get(f"/api/warehouse/materials/{self.sheet.id}/").data
        self.assertGreater(Decimal(str(row["stock_value"])), Decimal("0"))
        # Админ по-прежнему правит закупочную цену через карточку.
        r = self.client.patch(f"/api/warehouse/materials/{self.sheet.id}/", {"purchase_price": "999"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["purchase_price"])), Decimal("999"))
