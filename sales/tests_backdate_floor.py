"""Нижняя граница даты заказа.

Верхняя граница (будущее) была, нижней не было вовсе: `order_date=2015-01-01`
принимался, и опечатка в году переписывала выручку и складской лист месяца, в
который никто уже не заглядывает.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from sales.views import MAX_BACKDATE_DAYS
from warehouse.models import Material


class BackdateFloorTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="bd_admin", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.client.force_authenticate(self.admin)

    def _checkout(self, day):
        return self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "order_date": str(day),
            "items": [{"type": "MATERIAL", "material": self.mat.id, "quantity": 1}],
        }, format="json")

    def test_ancient_date_is_refused(self):
        r = self._checkout("2015-01-01")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("год", str(r.data).lower())

    def test_just_beyond_the_floor_is_refused(self):
        day = timezone.localdate() - timedelta(days=MAX_BACKDATE_DAYS + 1)
        self.assertEqual(self._checkout(day).status_code, 400)

    def test_within_the_floor_is_allowed(self):
        day = timezone.localdate() - timedelta(days=MAX_BACKDATE_DAYS - 1)
        r = self._checkout(day)
        self.assertEqual(r.status_code, 201, r.data)

    def test_yesterday_is_allowed(self):
        """Обычный случай: заказ занесли на следующий день."""
        day = timezone.localdate() - timedelta(days=1)
        self.assertEqual(self._checkout(day).status_code, 201)

    def test_future_is_still_refused(self):
        day = timezone.localdate() + timedelta(days=1)
        self.assertEqual(self._checkout(day).status_code, 400)

    def test_editing_a_receipt_cannot_move_it_that_far_back(self):
        made = self._checkout(timezone.localdate()).data
        r = self.client.patch(
            f"/api/sales/receipts/{made['id']}/",
            {"order_date": "2015-01-01"}, format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        receipt = Receipt.objects.get(pk=made["id"])
        self.assertEqual(
            timezone.localtime(receipt.created_at).date(), timezone.localdate()
        )
