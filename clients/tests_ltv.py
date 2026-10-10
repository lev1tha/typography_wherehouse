"""LTV клиента — только состоявшиеся продажи.

Неоплаченный онлайн-счёт «нигде не числится» (D-7, D-37): не выручка и не долг.
В «сумму покупок» карточки и рефералов он попадал, и цифра клиента расходилась
с отчётами.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from clients.serializers import client_ltv
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material

D = Decimal


class ClientLtvTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ltv_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.plate = Material.objects.create(
            name="Табличка", unit=Material.Unit.PIECE, quantity=D("1000"),
            purchase_price=D("100"), price_per_unit=D("300"),
        )
        self.boss = Client.objects.create(full_name="Привёл", phone="+996700000001")
        self.customer = Client.objects.create(
            full_name="Покупатель", phone="+996700000002", referred_by=self.boss
        )

    def sale(self, qty, method="CASH", paid=None):
        return sale_service.create_sale(
            client=self.customer, cashier=self.admin, payment_method=method,
            items_data=[{"type": "MATERIAL", "material": self.plate,
                         "quantity": D(qty), "mode": "PIECE"}],
            amount_paid=paid,
        )

    def test_unpaid_online_invoice_is_not_in_lifetime_value(self):
        self.sale(2, paid=D("600"))                       # 600 — состоявшаяся продажа
        online = self.sale(1, method="ONLINE", paid=None)  # счёт 300, не оплачен
        online.refresh_from_db()
        self.assertIsNone(online.revenue_recognized_at)

        r = self.client.get(f"/api/clients/clients/{self.customer.pk}/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["stats"]["lifetime_value"])), D("600"))
        self.assertEqual(Decimal(str(r.data["stats"]["gross"])), D("600"))
        self.assertEqual(client_ltv(self.customer), D("600"))

        boss = self.client.get(f"/api/clients/clients/{self.boss.pk}/")
        self.assertEqual(Decimal(str(boss.data["referrals"]["list"][0]["lifetime_value"])), D("600"))
        self.assertEqual(Decimal(str(boss.data["referrals"]["total_value"])), D("600"))

    def test_paid_online_order_counts_after_confirmation(self):
        online = self.sale(1, method="ONLINE", paid=None)
        sale_service.confirm_payment(online)
        self.assertEqual(client_ltv(self.customer), D("300"))

    def test_refund_is_subtracted(self):
        receipt = self.sale(2, paid=D("600"))
        sale_service.refund_receipt(receipt, item_ids=[receipt.items.get().id], user=self.admin)
        self.assertEqual(client_ltv(self.customer), D("0"))
        self.assertEqual(Receipt.objects.filter(client=self.customer).count(), 1)
