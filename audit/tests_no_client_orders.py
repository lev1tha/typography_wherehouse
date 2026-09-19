"""Заказы без клиента: считаются везде, но их видно отдельно.

Проверка прод-данных 19.09.2026. Четыре заказа оформили без имени и целиком в
долг. Из-за них две пары цифр не сходились, и обе выглядели неправильными,
хотя каждая считалась верно:

  · «Долг клиентов» в Финансах 314 141, а сумма долгов по карточкам — 304 038;
  · «Продали материала на» 474 274, а таблица покупок под ней — 467 263.

Теперь такой заказ виден строкой «Без клиента» в таблице и строкой «из них без
клиента» под долгом: спросить его не с кого, и это должно быть написано, а не
выясняться вычитанием.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material


class NoClientOrdersTests(APITestCase):
    PURCHASES = "/api/audit/client-purchases/"
    DASHBOARD = "/api/audit/dashboard/"
    REPORT = "/api/finance/report/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="nc_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.customer = Client.objects.create(full_name="Клиент", phone="+996700000444")
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE,
            quantity=Decimal("1000"), price_per_unit=Decimal("100"),
            purchase_price=Decimal("40"),
        )

    def _sale(self, *, client, qty, paid):
        return sale_service.create_sale(
            client=client, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal(qty), "mode": "SQM",
            }],
            amount_paid=Decimal(paid),
        )

    def test_walk_in_purchases_keep_the_table_equal_to_the_tile(self):
        self._sale(client=self.customer, qty="5", paid="500")
        self._sale(client=None, qty="3", paid="300")
        rows = self.client.get(self.PURCHASES).data
        total = sum((Decimal(str(r["material_spend"])) for r in rows), Decimal("0"))
        tile = self.client.get(self.DASHBOARD).data["breakdown"]["material_revenue"]
        self.assertEqual(total, Decimal(str(tile)))
        anon = [r for r in rows if r["client_id"] is None]
        self.assertEqual(len(anon), 1)
        self.assertEqual(Decimal(str(anon[0]["material_spend"])), Decimal("300"))
        self.assertEqual(anon[0]["orders"], 1)

    def test_no_walk_in_row_when_every_order_has_a_client(self):
        self._sale(client=self.customer, qty="5", paid="500")
        rows = self.client.get(self.PURCHASES).data
        self.assertTrue(all(r["client_id"] for r in rows))

    def test_debt_without_a_client_is_shown_separately(self):
        self._sale(client=self.customer, qty="10", paid="0")   # долг 1000
        self._sale(client=None, qty="4", paid="0")             # долг 400, спросить не с кого
        data = self.client.get(self.REPORT).data
        self.assertEqual(Decimal(str(data["client_debt"])), Decimal("1400"))
        self.assertEqual(Decimal(str(data["anonymous_debt"])), Decimal("400"))
        # Остаток — ровно то, что стоит в карточках клиентов.
        by_cards = sum(
            (r.debt for r in Receipt.objects.filter(client__isnull=False)), Decimal("0")
        )
        self.assertEqual(
            Decimal(str(data["client_debt"])) - Decimal(str(data["anonymous_debt"])),
            by_cards,
        )

    def test_paid_walk_in_order_adds_no_debt(self):
        self._sale(client=None, qty="4", paid="400")
        data = self.client.get(self.REPORT).data
        self.assertEqual(Decimal(str(data["anonymous_debt"])), Decimal("0"))
