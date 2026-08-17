"""Остаток не уходит в минус — ни продажей, ни дозаказом.

У площадных материалов нехватку давно ловит FIFO по партиям (`consume_area`),
а ШТУЧНЫЕ (крепёж, клей, бумага) списывались молча: продажа 10 000 штук при
остатке 484 создавала чек на 550 000 сом и остаток −9 519. Отрицательный
остаток ломает стоимость склада, себестоимость и метку «нет в наличии», а
заметить его можно только глазами в таблице.

Инвентаризация — единственное исключение: она не «списывает», а приравнивает
остаток к пересчитанному.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from warehouse.models import InventoryLog, Material


class NegativeStockTests(APITestCase):
    CHECKOUT = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ns_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.piece = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("10"),
            price_per_unit=Decimal("100"), purchase_price=Decimal("40"),
        )

    def _sale(self, qty):
        return self.client.post(self.CHECKOUT, {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.piece.id, "quantity": qty}],
        }, format="json")

    def test_sale_beyond_stock_is_rejected(self):
        resp = self._sale(11)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("недостаточно", str(resp.data).lower())
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("10"))
        # Ни чека, ни движения по складу после отказа не осталось.
        self.assertFalse(Receipt.objects.exists())
        self.assertFalse(InventoryLog.objects.filter(material=self.piece).exists())

    def test_sale_of_exactly_the_whole_stock_still_works(self):
        self.assertEqual(self._sale(10).status_code, 201)
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("0"))

    def test_top_up_beyond_stock_is_rejected(self):
        """Дозаказ — та же продажа, и остаток он тоже не должен пробивать."""
        sale = self._sale(2)
        self.assertEqual(sale.status_code, 201)
        resp = self.client.post(
            f"/api/sales/receipts/{sale.data['id']}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.piece.id, "quantity": 99}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("8"))

    def test_write_off_beyond_stock_is_rejected(self):
        resp = self.client.post("/api/warehouse/materials/write-off/", {
            "material": self.piece.id, "quantity": "50", "reason_code": "DAMAGE",
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("10"))

    def test_inventory_can_still_count_the_stock_down_to_zero(self):
        """Пересчитали и нашли ноль — это не «списание в минус», это правда."""
        resp = self.client.post("/api/warehouse/materials/adjust/", {
            "material": self.piece.id, "counted_quantity": "0", "reason": "пересчёт",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("0"))

    def test_intake_and_returns_are_untouched(self):
        self.assertEqual(self.client.post("/api/warehouse/materials/supply/", {
            "material": self.piece.id, "quantity": "5", "actual_price": "40",
        }, format="json").status_code, 200)
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("15"))

        sale = self._sale(15)
        self.assertEqual(sale.status_code, 201)
        self.assertEqual(
            self.client.post(f"/api/sales/receipts/{sale.data['id']}/refund/", {}, format="json").status_code,
            200,
        )
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("15"))
