"""Сдача по возвращённому целиком заказу считается везде одинаково.

Заказ 1500, принесли 3000, сдачу не выдали, заказ вернули целиком. Деньги по
нему лежат в кассе, и зачёт в новый заказ эту сдачу видит (1500), — а плитка
сдачи в списке чеков, карточка клиента и «Касса» отсекали отменённые чеки и
показывали 0. Одно правило: отменённые и возвращённые чеки при подсчёте
оставшейся сдачи НЕ фильтруются.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from sales.sale_service import client_change_available, create_sale, refund_receipt
from warehouse.models import Material


class ChangeAfterWholeReturnTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="car_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.person = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("1000"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        self.order = create_sale(
            client=self.person, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": 15}],
            amount_paid=Decimal("3000"),
        )
        refund_receipt(self.order, user=self.admin)
        self.order.refresh_from_db()

    def test_setup_the_order_is_cancelled_but_the_change_is_still_owed(self):
        self.assertEqual(self.order.status, Receipt.Status.CANCELLED)
        self.assertEqual(self.order.change_due, Decimal("1500"))
        self.assertEqual(client_change_available(self.person), Decimal("1500"))

    def test_receipts_tile_shows_it(self):
        resp = self.client.get("/api/sales/receipts/stats/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Decimal(str(resp.data["change_due"])), Decimal("1500"))

    def test_client_card_shows_it(self):
        resp = self.client.get(f"/api/clients/clients/{self.person.id}/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Decimal(str(resp.data["change_due"])), Decimal("1500"))

    def test_clients_list_shows_it(self):
        resp = self.client.get("/api/clients/clients/")
        rows = resp.data["results"] if isinstance(resp.data, dict) else resp.data
        row = next(r for r in rows if r["id"] == self.person.id)
        self.assertEqual(Decimal(str(row["change_due"])), Decimal("1500"))

    def test_cash_screen_shows_it(self):
        resp = self.client.get("/api/finance/cash/balance/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Decimal(str(resp.data["change_held"])), Decimal("1500"))
