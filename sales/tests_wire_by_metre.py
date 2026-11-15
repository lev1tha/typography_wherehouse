"""Провод и прочий товар без ширины продаётся погонными метрами.

Его заводили рулоном и в «ширину рулона» писали длину бухты (250 м) — карточка
отказывала «больше 4 м». Правильная форма — штучная с единицей «пог.м»:
приход метрами, продажа дробными метрами, бухта не нужна.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material


class WireByMetreTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="wire_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)

    def test_card_supply_and_fractional_sale(self):
        r = self.client.post("/api/warehouse/materials/", {
            "name": "провод 0,75 мм", "unit": "METER", "is_roll_material": False,
            "purchase_price": "23", "price_per_unit": "30",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        mat = Material.objects.get(pk=r.data["id"])
        self.assertEqual(mat.unit, Material.Unit.METER)

        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": mat.id, "quantity": 250, "actual_price": 23, "payment": "DEBT",
        }, format="json")
        self.assertIn(r.status_code, (200, 201), r.data)

        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": mat.id, "quantity": "12.5"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("375"))
        mat.refresh_from_db()
        self.assertEqual(mat.quantity, Decimal("237.5"))

    def test_till_sends_piece_mode_and_fractional_metres_pass(self):
        """Касса шлёт любой штучный материал с mode=PIECE — у метров и литров
        дробное количество при этом законно."""
        mat = Material.objects.create(
            name="провод", unit=Material.Unit.METER, quantity=Decimal("250"),
            price_per_unit=Decimal("30"), purchase_price=Decimal("23"),
        )
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": mat.id, "quantity": "12.5", "mode": "PIECE"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
