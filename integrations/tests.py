"""Заглушка платёжного шлюза не должна подтверждать оплату вне разработки.

У `MockGateway` нет ни подписи, ни секрета — «оплата прошла» она отвечает на
любой запрос. Вебхук при этом открыт наружу без авторизации, а на проде шлюз до
сих пор `mock` (ключей FreedomPay нет). То есть достаточно было знать
идентификатор чека — тот самый, что система сама отдаёт клиенту в ссылке на
оплату, — чтобы закрыть заказ как оплаченный и списать склад.
"""
from decimal import Decimal

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from warehouse.models import Material


class MockGatewayWebhookTests(APITestCase):
    URL = "/api/integrations/payments/webhook/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="pay_admin", password="x", role=User.Role.ADMIN
        )
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "ONLINE",
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 2}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.receipt = Receipt.objects.get(pk=r.data["id"])
        self.reference = self.receipt.payment_reference
        # Заказ создан неоплаченным, склад ещё не тронут.
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.client.force_authenticate(None)

    @override_settings(DEBUG=False, PAYMENT_GATEWAY="mock")
    def test_anonymous_webhook_cannot_settle_an_order_in_production(self):
        resp = self.client.post(self.URL, {"reference": self.reference}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(self.receipt.amount_paid, Decimal("0"))
        # Главное: склад не списан за «оплату», которой не было.
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("100"))

    @override_settings(DEBUG=True, PAYMENT_GATEWAY="mock")
    def test_webhook_still_works_for_local_development(self):
        resp = self.client.post(self.URL, {"reference": self.reference}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PAID)

    @override_settings(DEBUG=False, PAYMENT_GATEWAY="mock")
    def test_unknown_reference_is_refused_too(self):
        resp = self.client.post(self.URL, {"reference": "MOCK-нет-такого"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)


class TelegramWebhookSecretTests(APITestCase):
    """Вебхук клиентского бота требует секрет.

    Проверки не было вовсе: зная номер клиента, любой привязывал его карточку к
    своему чату — и туда начинали уходить его чеки с суммами.
    """

    URL = "/api/integrations/telegram/customer/webhook/"

    def setUp(self):
        from clients.models import Client

        self.customer = Client.objects.create(
            full_name="Тахир", phone="+996555111222"
        )

    def _post(self, **extra):
        return self.client.post(self.URL, {
            "message": {
                "chat": {"id": 777},
                "contact": {"phone_number": "996555111222"},
            }
        }, format="json", **extra)

    @override_settings(TELEGRAM_WEBHOOK_SECRET="верный-секрет")
    def test_without_the_secret_nothing_is_linked(self):
        r = self._post()
        self.assertEqual(r.status_code, 403, r.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.telegram_chat_id)

    @override_settings(TELEGRAM_WEBHOOK_SECRET="верный-секрет")
    def test_wrong_secret_is_refused(self):
        r = self._post(HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="чужой")
        self.assertEqual(r.status_code, 403)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.telegram_chat_id)

    @override_settings(TELEGRAM_WEBHOOK_SECRET="верный-секрет")
    def test_correct_secret_links_the_chat(self):
        r = self._post(HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="верный-секрет")
        self.assertEqual(r.status_code, 200, r.data)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.telegram_chat_id, "777")

    @override_settings(TELEGRAM_WEBHOOK_SECRET="")
    def test_unconfigured_webhook_is_closed(self):
        """Секрет не настроен — дверь закрыта совсем, а не «пока откроем»."""
        r = self._post(HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="что угодно")
        self.assertEqual(r.status_code, 403)
