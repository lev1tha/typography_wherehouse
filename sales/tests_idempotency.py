"""Идемпотентность: заголовок `Idempotency-Key` на оформлении чека и на /pay/.

Касса на плохой сети повторяет запрос, не дождавшись ответа, — без ключа
получался второй чек или вторая оплата. Ключ необязателен; без заголовка всё
работает как раньше.
"""
import threading
from datetime import timedelta
from decimal import Decimal

from django.db import connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from finance.models import CashEntry
from sales import idempotency
from sales.models import IdempotencyRecord, Payment, Receipt
from sales.sale_service import create_sale
from warehouse.models import Material

CHECKOUT = "/api/sales/receipts/checkout/"


def _payload(material, qty=2, **extra):
    return {
        "payment_method": "CASH", "pay_full": True,
        "items": [{"type": "MATERIAL", "material": material.id, "quantity": qty}],
        **extra,
    }


class CheckoutIdempotencyTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="idem_boss", password="x", role=User.Role.ADMIN
        )
        self.other = User.objects.create_user(
            username="idem_other", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("10"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )

    def _post(self, key=None, **kwargs):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(CHECKOUT, _payload(self.mat, **kwargs), format="json", **headers)

    def test_repeat_with_the_same_key_returns_the_same_receipt(self):
        first = self._post("11111111-aaaa")
        second = self._post("11111111-aaaa")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(second["Idempotent-Replay"], "true")
        self.assertTrue(second.data["idempotent_replay"])
        self.assertEqual(Receipt.objects.count(), 1)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("8"))   # списано один раз
        self.assertEqual(CashEntry.objects.filter(kind="IN").count(), 1)

    def test_without_a_key_nothing_changes(self):
        self._post()
        self._post()
        self.assertEqual(Receipt.objects.count(), 2)

    def test_different_keys_are_different_orders(self):
        self._post("key-one")
        self._post("key-two")
        self.assertEqual(Receipt.objects.count(), 2)

    def test_keys_are_per_user(self):
        self._post("shared-key")
        self.client.force_authenticate(self.other)
        resp = self._post("shared-key")
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn("Idempotent-Replay", resp)
        self.assertEqual(Receipt.objects.count(), 2)

    def test_a_failed_attempt_does_not_burn_the_key(self):
        too_many = self._post("retry-key", qty=500)
        self.assertEqual(too_many.status_code, 400, too_many.data)
        self.assertFalse(IdempotencyRecord.objects.exists())
        ok = self._post("retry-key", qty=2)
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(Receipt.objects.count(), 1)

    def test_a_garbage_key_is_refused(self):
        resp = self._post("x" * 101)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(self._post("a b c").status_code, 400)
        self.assertFalse(Receipt.objects.exists())

    def test_replay_of_a_deleted_receipt_is_a_conflict(self):
        first = self._post("gone-key")
        self.client.delete(f"/api/sales/receipts/{first.data['id']}/")
        resp = self._post("gone-key")
        self.assertEqual(resp.status_code, 409, resp.data)

    def test_old_records_are_swept_lazily(self):
        old = IdempotencyRecord.objects.create(
            user=self.admin, endpoint="checkout", key="ancient",
            created_at=timezone.now() - timedelta(days=8),
        )
        fresh = IdempotencyRecord.objects.create(
            user=self.admin, endpoint="checkout", key="recent",
            created_at=timezone.now() - timedelta(days=6),
        )
        self._post("brand-new")
        self.assertFalse(IdempotencyRecord.objects.filter(pk=old.pk).exists())
        self.assertTrue(IdempotencyRecord.objects.filter(pk=fresh.pk).exists())


class PayIdempotencyTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="idemp_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        self.a = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": mat, "quantity": 5}],
        )
        self.b = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": mat, "quantity": 5}],
        )

    def _pay(self, receipt, key=None, amount="100"):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(
            f"/api/sales/receipts/{receipt.id}/pay/", {"amount": amount},
            format="json", **headers,
        )

    def test_repeat_with_the_same_key_takes_the_payment_once(self):
        first = self._pay(self.a, "pay-1")
        second = self._pay(self.a, "pay-1")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(Payment.objects.filter(receipt=self.a).count(), 1)
        self.a.refresh_from_db()
        self.assertEqual(self.a.amount_paid, Decimal("100"))
        self.assertEqual(Decimal(second.data["amount_paid"]), Decimal("100"))
        self.assertEqual(second["Idempotent-Replay"], "true")

    def test_without_a_key_a_second_payment_is_a_second_payment(self):
        self._pay(self.a)
        self._pay(self.a)
        self.a.refresh_from_db()
        self.assertEqual(self.a.amount_paid, Decimal("200"))

    def test_the_same_key_on_another_receipt_is_not_a_replay(self):
        self._pay(self.a, "shared")
        resp = self._pay(self.b, "shared")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Payment.objects.count(), 2)


@skipUnlessDBFeature("has_select_for_update")
class ParallelIdempotencyTests(TransactionTestCase):
    """Два одновременных запроса с одним ключом — один чек (только PostgreSQL)."""

    def test_parallel_checkouts_with_one_key_make_one_receipt(self):
        admin = User.objects.create_user(
            username="idem_par", password="x", role=User.Role.ADMIN
        )
        mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("10"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        barrier = threading.Barrier(4)
        out = [None] * 4

        def worker(i):
            try:
                api = APIClient()
                api.force_authenticate(User.objects.get(pk=admin.pk))
                barrier.wait(timeout=10)
                out[i] = api.post(
                    CHECKOUT, _payload(mat), format="json",
                    HTTP_IDEMPOTENCY_KEY="parallel-key",
                ).status_code
            except Exception as exc:  # noqa: BLE001
                out[i] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(out, [201, 201, 201, 201])
        self.assertEqual(Receipt.objects.count(), 1)
        mat.refresh_from_db()
        self.assertEqual(mat.quantity, Decimal("8"))


class KeyParsingTests(APITestCase):
    def test_valid_shapes(self):
        from rest_framework.test import APIRequestFactory
        from rest_framework.request import Request

        factory = APIRequestFactory()
        for ok in ("550e8400-e29b-41d4-a716-446655440000", "abc_1.2:3-x"):
            req = Request(factory.post("/x/", HTTP_IDEMPOTENCY_KEY=ok))
            self.assertEqual(idempotency.key_from(req), ok)
        self.assertIsNone(idempotency.key_from(Request(factory.post("/x/"))))
