"""Правки по ревизии: финансовый пароль и согласованность плитки «Выручка»."""
from decimal import Decimal

from django.core import signing
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.views import _finance_signer
from sales.models import Receipt


@override_settings(FINANCE_PASSWORD="секрет123")
class FinanceUnlockTokenTests(APITestCase):
    """Снятие пароля подтверждает СЕРВЕР, а не отметка времени в браузере.

    Раньше фронтенд хранил `financeUnlockedAt` — обычное число, — и строки
    `localStorage.setItem('financeUnlockedAt', Date.now())` хватало, чтобы
    открыть «Финансы», не зная пароля.
    """

    URL = "/api/finance/unlock/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="fu_admin", password="x", role=User.Role.ADMIN
        )
        self.other = User.objects.create_user(
            username="fu_buh", password="x", role=User.Role.ACCOUNTANT
        )

    def _unlock(self, user, password="секрет123"):
        self.client.force_authenticate(user)
        return self.client.post(self.URL, {"password": password}, format="json")

    def test_correct_password_returns_a_signed_token(self):
        r = self._unlock(self.admin)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["ok"])
        # Токен подписан: без SECRET_KEY его не сочинить.
        self.assertEqual(
            _finance_signer.unsign(r.data["token"], max_age=60), str(self.admin.pk)
        )

    def test_wrong_password_gives_no_token(self):
        r = self._unlock(self.admin, "не тот")
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("token", r.data)

    def test_server_confirms_a_real_token(self):
        token = self._unlock(self.admin).data["token"]
        r = self.client.get(self.URL, HTTP_X_FINANCE_UNLOCK=token)
        self.assertTrue(r.data["ok"])

    def test_made_up_token_is_refused(self):
        self.client.force_authenticate(self.admin)
        for fake in ("", "1786995476527", "что-угодно", "1:abc"):
            r = self.client.get(self.URL, HTTP_X_FINANCE_UNLOCK=fake)
            self.assertFalse(r.data["ok"], f"подделка прошла: {fake!r}")

    def test_token_of_another_user_does_not_open_the_section(self):
        """Признак именной: разблокировка одного не открывает раздел тому,
        кто сядет за ту же машину следующим."""
        token = self._unlock(self.admin).data["token"]
        self.client.force_authenticate(self.other)
        r = self.client.get(self.URL, HTTP_X_FINANCE_UNLOCK=token)
        self.assertFalse(r.data["ok"])

    def test_expired_token_is_refused(self):
        old = signing.TimestampSigner(salt="finance-unlock").sign(str(self.admin.pk))
        self.client.force_authenticate(self.admin)
        with override_settings(FINANCE_PASSWORD="секрет123"):
            # Подменяем срок жизни на нулевой — токен мгновенно просрочен.
            import finance.views as fv

            saved, fv.FINANCE_UNLOCK_TTL = fv.FINANCE_UNLOCK_TTL, -1
            try:
                r = self.client.get(self.URL, HTTP_X_FINANCE_UNLOCK=old)
            finally:
                fv.FINANCE_UNLOCK_TTL = saved
        self.assertFalse(r.data["ok"])


class RevenuePaidTests(APITestCase):
    """«Оплачено» не может быть больше «Выручки».

    `revenue` вычитает возвраты, а `revenue_paid` их не вычитал: плитка
    показывала «Выручка 13 253 · Оплачено: 13 613 · В долг: 0» и сама себе
    противоречила.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="rp_admin", password="x", role=User.Role.ADMIN
        )
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555777111")
        self.client.force_authenticate(self.admin)

    def _report(self):
        return self.client.get("/api/finance/report/").data

    def test_paid_never_exceeds_revenue_after_a_partial_refund(self):
        Receipt.objects.create(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            payment_status=Receipt.PaymentStatus.PARTIALLY_REFUNDED,
            total_price=Decimal("894"), amount_paid=Decimal("804"),
            refunded_amount=Decimal("450"),
        )
        rep = self._report()
        self.assertLessEqual(
            Decimal(str(rep["revenue_paid"])), Decimal(str(rep["revenue"])),
            "«оплачено» вылезло выше выручки",
        )
        # Осталось строк на 444 — столько и денег по ним.
        self.assertEqual(Decimal(str(rep["revenue_paid"])), Decimal("444"))

    def test_paid_plus_debt_adds_up_to_revenue(self):
        Receipt.objects.create(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            payment_status=Receipt.PaymentStatus.PENDING,
            total_price=Decimal("1000"), amount_paid=Decimal("400"),
        )
        rep = self._report()
        self.assertEqual(
            Decimal(str(rep["revenue_paid"])) + Decimal(str(rep["client_debt"])),
            Decimal(str(rep["revenue"])),
        )
