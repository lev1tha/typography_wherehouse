"""Голый POST /api/sales/receipts/ закрыт — чеки появляются только через кассу.

ModelViewSet давал create бесплатно: API принимал чек без позиций, но с
«принятой» суммой (позиции у сериализатора read-only, amount_paid — нет).
Получался заказ-призрак: итог 0, оплачено 4000, номер занят.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt


class NoBareReceiptCreateTests(APITestCase):
    URL = "/api/sales/receipts/"

    def setUp(self):
        self.admin = User.objects.create_user(username="nb_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def test_bare_create_is_rejected_and_nothing_is_written(self):
        r = self.client.post(self.URL, {
            "payment_method": "CASH",
            "amount_paid": "4000",
        }, format="json")
        self.assertEqual(r.status_code, 405, r.data)
        self.assertIn("checkout", r.data["detail"])
        self.assertEqual(Receipt.objects.count(), 0)

    def test_checkout_still_works(self):
        r = self.client.post(f"{self.URL}checkout/", {
            "payment_method": "CASH",
            "items": [],
        }, format="json")
        # Пустая корзина — отказ по существу (400), а не 405: сам путь живой.
        self.assertEqual(r.status_code, 400, r.data)

    def test_storekeeper_gets_the_same_405(self):
        keeper = User.objects.create_user(username="nb_keeper", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(keeper)
        r = self.client.post(self.URL, {"payment_method": "CASH"}, format="json")
        self.assertEqual(r.status_code, 405)
