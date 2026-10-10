"""Список чеков не делает запрос на каждую строку.

30 запросов на 25 чеков: признак «есть услуга» (`Receipt.has_service`) и
партия строки (`roll_label`) тянулись по запросу на каждый чек. Теперь признак
считается аннотацией, а партии подгружаются заранее.
"""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from sales.sale_service import apply_payment, create_sale
from services.models import PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class ReceiptListQueryCountTests(APITestCase):
    URL = "/api/sales/receipts/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="lq_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        person = Client.objects.create(full_name="Тахир", phone="+996555111222")
        bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("1000"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        acrylic = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"), purchase_price=Decimal("600"),
        )
        receive_lot(
            acrylic, form=Roll.Form.SHEET, purchase_cost=Decimal("60000"),
            width=Decimal("1"), height=Decimal("1"), sheet_count=200, user=self.admin,
        )
        service = PrintingService.objects.create(
            name="Прочее", kind=PrintingService.Kind.OTHER, base_price=Decimal("500")
        )
        for i in range(25):
            receipt = create_sale(
                client=person if i % 2 else None, cashier=self.admin,
                payment_method=Receipt.PaymentMethod.CASH,
                items_data=[
                    {"type": "MATERIAL", "material": bolts, "quantity": 2},
                    {"type": "MATERIAL", "material": acrylic, "quantity": Decimal("1"),
                     "mode": "SQM"},
                    *([{"type": "SERVICE", "service": service, "quantity": 1}] if i % 3 == 0 else []),
                ],
                amount_paid=Decimal("100"),
            )
            apply_payment(receipt, Decimal("50"), user=self.admin)

    def test_query_count_does_not_grow_with_the_page(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["results"]), 25)
        # Было 56 на этой странице (по запросу на чек); стало 7 — запросы
        # страницы, а не строк.
        self.assertLessEqual(len(ctx), 12, [q["sql"][:90] for q in ctx])

    def test_has_service_is_right(self):
        resp = self.client.get(self.URL, {"page_size": 100})
        flags = {r["order_number"]: r["has_service"] for r in resp.data["results"]}
        # Услуга — в каждом третьем чеке (i % 3 == 0 → номера 1, 4, 7, …).
        self.assertEqual(sum(flags.values()), 9)
        self.assertTrue(flags[1])
        self.assertFalse(flags[2])
