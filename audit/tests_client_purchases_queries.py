"""«Покупки клиентов»: число запросов не растёт с числом клиентов.

Раньше на каждого клиента шёл свой `COUNT` заказов — на 50 клиентах 54 запроса,
на 800 — 803. Теперь заказы считаются одним GROUP BY.
"""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from warehouse.models import Material

D = Decimal
URL = "/api/audit/client-purchases/"


class ClientPurchasesQueryTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="cp_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.plate = Material.objects.create(
            name="Табличка", unit=Material.Unit.PIECE, quantity=D("10000"),
            purchase_price=D("100"), price_per_unit=D("300"),
        )

    def make_clients(self, n, start=0):
        for i in range(start, start + n):
            c = Client.objects.create(full_name=f"Клиент {i}", phone=f"+9967{i:08d}")
            for q in (1, 2):
                sale_service.create_sale(
                    client=c, cashier=self.admin, payment_method="CASH",
                    items_data=[{"type": "MATERIAL", "material": self.plate,
                                 "quantity": D(q), "mode": "PIECE"}],
                    amount_paid=None,
                )

    def count_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        return len(ctx.captured_queries), r.json()

    def test_query_count_does_not_grow_with_clients(self):
        self.make_clients(5)
        small, rows = self.count_queries()
        self.make_clients(25, start=5)
        large, rows_large = self.count_queries()
        self.assertEqual(small, large)
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(rows_large), 30)

    def test_numbers_are_right(self):
        self.make_clients(3)
        _, rows = self.count_queries()
        for row in rows:
            self.assertEqual(row["orders"], 2)
            self.assertEqual(D(str(row["material_spend"])), D("900"))      # 300 + 600
            self.assertEqual(D(str(row["material_qty"])), D("3"))

    def test_street_sales_get_their_own_row_with_a_count(self):
        sale_service.create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.plate, "quantity": D(1), "mode": "PIECE"}],
            amount_paid=None,
        )
        _, rows = self.count_queries()
        self.assertEqual(rows[0]["client_name"], "Без клиента")
        self.assertEqual(rows[0]["orders"], 1)
