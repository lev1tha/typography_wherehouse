"""Заказы в кабинете клиента: описание работы, единица, оплачено/возвращено.

Строка гравировки приезжала как «Гравировка × 0.48» — без описания и без
единицы. Теперь название собирается как у сотрудника (`itemTitle`: «имя — описание»),
а количество идёт с единицей («кв.м»).
"""
from decimal import Decimal

from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from services.models import PrintingService
from warehouse.models import Material

D = Decimal


class PortalOrderShapeTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="pi_admin", password="x", role=User.Role.ADMIN)
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555123456")
        self.customer.set_password("portal-1")
        self.customer.save()
        self.engraving = PrintingService.objects.get(kind=PrintingService.Kind.ENGRAVING)
        self.engraving.rate_flat = D("3000")
        self.engraving.save()
        self.screws = Material.objects.create(
            name="Саморезы", unit=Material.Unit.PIECE, quantity=D("1000"),
            price_per_unit=D("5"), purchase_price=D("2"),
        )

    def tearDown(self):
        cache.clear()

    def orders(self):
        login = self.client.post(
            "/api/customer/login/", {"phone": "+996555123456", "password": "portal-1"}, format="json"
        )
        r = self.client.get("/api/customer/orders/", HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_engraving_line_has_description_and_square_metres(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "client_id": self.customer.pk,
            "items": [
                {"type": "SERVICE", "service": self.engraving.id, "width": "0.8", "length": "0.6",
                 "note": "логотип на двери"},
                {"type": "MATERIAL", "material": self.screws.id, "quantity": 100, "mode": "PIECE"},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.client.force_authenticate(None)

        order = self.orders()[0]
        by_title = {i["title"]: i for i in order["items"]}
        engr = by_title["Гравировка — логотип на двери"]
        self.assertEqual(D(str(engr["quantity"])), D("0.48"))
        self.assertEqual(engr["unit"], "SQM")
        self.assertEqual(engr["unit_label"], "кв.м")
        self.assertEqual(engr["note"], "логотип на двери")
        pieces = by_title["Саморезы"]
        self.assertEqual((pieces["unit"], pieces["unit_label"]), ("PIECE", "шт"))

    def test_order_carries_paid_and_refunded_amounts(self):
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.screws, "quantity": D(10), "mode": "PIECE"}],
            amount_paid=D("30"),
        )
        order = self.orders()[0]
        self.assertEqual(D(str(order["amount_paid"])), D("30"))
        self.assertEqual(D(str(order["refunded_amount"])), D("0"))
        sale_service.refund_receipt(receipt, item_ids=[receipt.items.get().id], user=self.admin)
        order = self.orders()[0]
        self.assertEqual(D(str(order["refunded_amount"])), D("50"))
        self.assertTrue(order["items"][0]["is_returned"])

    def test_keys_are_stable(self):
        sale_service.create_sale(
            client=self.customer, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.screws, "quantity": D(1), "mode": "PIECE"}],
            amount_paid=None,
        )
        order = self.orders()[0]
        self.assertTrue({"amount_paid", "refunded_amount", "debt", "change_due", "items"} <= set(order))
        self.assertTrue(
            {"title", "note", "own_material", "quantity", "line_total", "unit", "unit_label", "is_returned"}
            <= set(order["items"][0])
        )
