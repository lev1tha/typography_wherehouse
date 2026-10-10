"""Вебхук FreedomPay: подпись, сумма, повтор.

Подпись защищает от подделки, но не от платежа на другую сумму: счёт на 1000,
а шлюз подтвердил 100 — и заказ закрывался как оплаченный целиком. Теперь
`pg_amount` обязан совпасть с итогом чека, подпись сравнивается за постоянное
время, а повторное подтверждение не списывает склад второй раз.
"""
from decimal import Decimal

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry
from integrations.payments import FreedomPayGateway
from sales.models import Receipt
from sales.sale_service import create_sale
from warehouse.models import Material

URL = "/api/integrations/payments/webhook/"
SECRET = "merchant-secret"


@override_settings(
    PAYMENT_GATEWAY="freedompay", PAYMENT_API_KEY="12345", PAYMENT_API_SECRET=SECRET,
    DEBUG=False,
)
class FreedomPayWebhookTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="fp_boss", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        # Счёт выставляем заглушкой: настоящий шлюз пошёл бы в сеть. Ссылку
        # платежа, как её присылает FreedomPay, задаём сами.
        with override_settings(PAYMENT_GATEWAY="mock"):
            self.receipt = create_sale(
                client=None, cashier=self.admin,
                payment_method=Receipt.PaymentMethod.ONLINE,
                items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": 10}],
            )
        self.receipt.payment_reference = "pay-777"
        self.receipt.save(update_fields=["payment_reference"])

    def _body(self, **override):
        data = {
            "pg_order_id": str(self.receipt.id), "pg_payment_id": "pay-777",
            "pg_result": "1", "pg_amount": "1000", "pg_salt": "abc",
            **override,
        }
        data = {k: v for k, v in data.items() if v is not None}
        data["pg_sig"] = FreedomPayGateway()._sign("webhook", data)
        return data

    def _post(self, body):
        return self.client.post(URL, body, format="json")

    def _stock(self):
        self.mat.refresh_from_db()
        return self.mat.quantity

    def test_correct_signature_and_amount_settle_the_order(self):
        resp = self._post(self._body())
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"<pg_status>ok</pg_status>", resp.content)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(self._stock(), Decimal("90"))
        self.assertEqual(CashEntry.objects.get().amount, Decimal("1000"))

    def test_a_wrong_signature_is_ignored(self):
        body = self._body()
        body["pg_sig"] = "0" * 32
        resp = self._post(body)
        self.assertIn(b"<pg_status>error</pg_status>", resp.content)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(self._stock(), Decimal("100"))

    def test_a_missing_or_non_ascii_signature_is_an_error_not_a_crash(self):
        body = self._body()
        del body["pg_sig"]
        self.assertIn(b"<pg_status>error</pg_status>", self._post(body).content)
        body["pg_sig"] = "подпись"
        self.assertIn(b"<pg_status>error</pg_status>", self._post(body).content)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_a_tampered_field_breaks_the_signature(self):
        body = self._body()
        body["pg_amount"] = "1"          # подпись считалась на 1000
        self.assertIn(b"<pg_status>error</pg_status>", self._post(body).content)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_a_validly_signed_payment_of_another_amount_is_refused(self):
        resp = self._post(self._body(pg_amount="100"))
        self.assertIn(b"amount mismatch", resp.content)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(self._stock(), Decimal("100"))
        self.assertFalse(CashEntry.objects.exists())

    def test_a_signed_payment_without_an_amount_is_refused(self):
        resp = self._post(self._body(pg_amount=None))
        self.assertIn(b"amount mismatch", resp.content)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_amount_written_with_decimals_still_matches(self):
        resp = self._post(self._body(pg_amount="1000.00"))
        self.assertIn(b"<pg_status>ok</pg_status>", resp.content)

    def test_unpaid_result_does_nothing(self):
        resp = self._post(self._body(pg_result="0"))
        self.assertIn(b"not paid", resp.content)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_a_repeated_confirmation_does_not_double_anything(self):
        self._post(self._body())
        self._post(self._body())
        self.assertEqual(self._stock(), Decimal("90"))
        self.assertEqual(CashEntry.objects.count(), 1)

    def test_an_unknown_or_empty_reference_matches_nothing(self):
        cash = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": 1}],
        )  # наличный, не оплачен, `payment_reference` пустой
        resp = self._post(self._body(pg_payment_id=""))
        self.assertIn(b"unknown receipt", resp.content)
        cash.refresh_from_db()
        self.assertEqual(cash.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertIn(b"unknown receipt", self._post(self._body(pg_payment_id="nope")).content)

    def test_a_cancelled_invoice_is_not_resurrected_by_the_gateway(self):
        from sales.sale_service import refund_receipt

        refund_receipt(Receipt.objects.get(pk=self.receipt.pk), user=self.admin)
        resp = self._post(self._body())
        self.assertIn(b"<pg_status>ok</pg_status>", resp.content)   # шлюзу — ack
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, Receipt.Status.CANCELLED)
        self.assertEqual(self._stock(), Decimal("100"))
