"""Общая обвязка тестов клиентов: пользователи, «монета» по 1 сому и продажа
на любую сумму.

Не тест: ничего не запускается, импортируется из `tests_*.py`. Материал-монета
стоит 1 сом за штуку, поэтому сумма заказа равна числу штук — заказ на 24 000
получается одной строкой, без каталога цен и правил прайса.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from warehouse.models import Material

D = Decimal


class ShopCase(APITestCase):
    """Админ, складовщик, бухгалтер, клиент-ОсОО и материал-«монета»."""

    def setUp(self):
        self.admin = User.objects.create_user(username="k_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="k_store", password="x", role=User.Role.STOREKEEPER)
        self.acc = User.objects.create_user(username="k_acc", password="x", role=User.Role.ACCOUNTANT)
        self.coin = Material.objects.create(
            name="Монета", unit=Material.Unit.PIECE, quantity=D("100000000"),
            price_per_unit=D("1"), purchase_price=D("0.4"),
        )
        self.agency = Client.objects.create(
            type=Client.Type.OSOO, company_name="ОсОО «Ак Жол»", phone="+996555112233",
            inn="02505201910136",
        )
        self.client.force_authenticate(self.admin)

    # --- продажи ------------------------------------------------------------
    def sale(self, total, *, client=None, paid=None, on=None, days_ago=0, method="CASH", **kw):
        """Заказ на `total` сом. `on` — дата заказа, `days_ago` — от сегодня."""
        when = None
        if on is not None:
            when = sale_service.day_to_moment(on)
        elif days_ago:
            when = sale_service.day_to_moment(timezone.localdate() - timedelta(days=days_ago))
        return sale_service.create_sale(
            client=client or self.agency, cashier=self.admin, payment_method=method,
            items_data=[{"type": "MATERIAL", "material": self.coin,
                         "quantity": D(str(total)), "mode": "PIECE"}],
            amount_paid=None if paid is None else D(str(paid)),
            created_at=when, **kw,
        )

    def pay(self, receipt, amount, on=None, method="CASH"):
        return sale_service.apply_payment(
            receipt, D(str(amount)), user=self.admin, paid_on=on, method=method,
        )

    def card(self, client=None):
        client = client or self.agency
        r = self.client.get(f"/api/clients/clients/{client.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def statement(self, client=None, **params):
        client = client or self.agency
        r = self.client.get(f"/api/clients/clients/{client.id}/statement/", params)
        self.assertEqual(r.status_code, 200, getattr(r, "data", r))
        return r.data
