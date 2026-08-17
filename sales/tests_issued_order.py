"""Выданный заказ дозаказу не подлежит.

Товар уже у клиента, чек на руках. Раньше проверялся только статус ОПЛАТЫ, и в
отданный заказ спокойно дописывались позиции: сумма росла со 110 до 165, склад
списывался, а у клиента оставалась бумага на старую сумму. Нужен ещё товар —
это новый заказ.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from warehouse.models import Material


class IssuedOrderTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="io_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 2}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.receipt_id = r.data["id"]

    def _add(self):
        return self.client.post(f"/api/sales/receipts/{self.receipt_id}/add-items/", {
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 1}],
        }, format="json")

    def test_top_up_works_while_the_order_is_in_the_shop(self):
        self.assertEqual(self._add().status_code, 200)
        self.assertEqual(
            Receipt.objects.get(id=self.receipt_id).total_price, Decimal("30")
        )

    def test_top_up_still_works_when_ready_but_not_handed_over(self):
        self.client.post(f"/api/sales/receipts/{self.receipt_id}/mark-ready/", {}, format="json")
        self.assertEqual(self._add().status_code, 200)

    def test_top_up_is_refused_after_the_order_was_handed_over(self):
        self.client.post(f"/api/sales/receipts/{self.receipt_id}/mark-ready/", {}, format="json")
        self.client.post(f"/api/sales/receipts/{self.receipt_id}/mark-issued/", {}, format="json")
        before = Receipt.objects.get(id=self.receipt_id).total_price
        resp = self._add()
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("выдан", str(resp.data).lower())
        self.assertEqual(Receipt.objects.get(id=self.receipt_id).total_price, before)
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("98"))
