"""Мелочи, на которых спотыкается требовательный глаз.

Пустой чек на 0 сом, «2,5 штуки крепежа» и возврат, который отвечает «успешно»,
ничего не сделав, — каждая по отдельности не авария, но именно из таких мелочей
складывается ощущение, что системе нельзя доверять.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from warehouse.models import Material


class InputGuardTests(APITestCase):
    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ig_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.piece = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.liquid = Material.objects.create(
            name="Клей", unit=Material.Unit.LITER, quantity=Decimal("20"),
            price_per_unit=Decimal("300"), purchase_price=Decimal("150"),
        )

    def _sale(self, material, qty, **extra):
        return self.client.post(self.URL, {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": material.id, "quantity": qty, **extra}],
        }, format="json")

    # ---- пустой чек ---------------------------------------------------
    def test_zero_quantity_does_not_create_an_empty_receipt(self):
        resp = self._sale(self.piece, 0)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(Receipt.objects.exists())

    def test_a_normal_sale_still_goes_through(self):
        self.assertEqual(self._sale(self.piece, 3).status_code, 201)

    def test_free_line_is_still_allowed(self):
        """Нулевая ЦЕНА законна — подарок или бесплатная доработка."""
        resp = self._sale(self.piece, 1, material_price="0")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Decimal(resp.data["total_price"]), Decimal("0"))

    # ---- дробные штуки -------------------------------------------------
    def test_pieces_are_sold_whole(self):
        resp = self._sale(self.piece, "2.5")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("целыми", str(resp.data).lower())

    def test_litres_can_be_fractional(self):
        self.assertEqual(self._sale(self.liquid, "1.5").status_code, 201)

    # ---- повторный возврат ---------------------------------------------
    def test_second_refund_is_refused_instead_of_answering_ok(self):
        sale = self._sale(self.piece, 2)
        self.assertEqual(sale.status_code, 201)
        first = self.client.post(f"/api/sales/receipts/{sale.data['id']}/refund/", {}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        second = self.client.post(f"/api/sales/receipts/{sale.data['id']}/refund/", {}, format="json")
        self.assertEqual(second.status_code, 400, second.data)
        self.assertIn("уже возвращены", str(second.data).lower())
        # Возвращённая сумма от повтора не удвоилась.
        self.assertEqual(
            Receipt.objects.get(id=sale.data["id"]).refunded_amount, Decimal("20")
        )
