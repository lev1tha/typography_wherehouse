"""Касса не уходит в минус и не принимает завтрашние деньги.

Из ящика нельзя выдать больше, чем в нём лежит: если система это позволяет,
значит она позволяет опечатку (лишний ноль, не тот счёт), а увидят её только
при сверке остатка. Дата в будущем — та же опечатка: остаток «на сегодня»
показывал бы деньги, которых ещё нет.

Наличные и банк считаются раздельно: денег в ящике может не быть, а на счёте —
быть, и наоборот.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry


class CashGuardTests(APITestCase):
    URL = "/api/finance/cash/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cg_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        CashEntry.objects.create(
            account=CashEntry.Account.CASH, kind=CashEntry.Kind.IN,
            article=CashEntry.Article.DEPOSIT, amount=Decimal("1000"),
        )

    def _out(self, amount, account=CashEntry.Account.CASH, **extra):
        return self.client.post(self.URL, {
            "account": account, "kind": "OUT", "article": "OTHER",
            "amount": str(amount), **extra,
        }, format="json")

    def test_payout_within_the_balance_passes(self):
        self.assertEqual(self._out(1000).status_code, 201)
        self.assertEqual(CashEntry.balance(CashEntry.Account.CASH), Decimal("0"))

    def test_payout_over_the_balance_asks_for_confirmation(self):
        resp = self._out(1001)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("подтвердите", str(resp.data).lower())
        self.assertEqual(CashEntry.balance(CashEntry.Account.CASH), Decimal("1000"))

    def test_confirmed_payout_over_the_balance_goes_through(self):
        """Кассу вносят не по порядку — запрещать наглухо нельзя."""
        resp = self._out(1001, confirm_negative=True)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(CashEntry.balance(CashEntry.Account.CASH), Decimal("-1"))

    def test_accounts_are_counted_separately(self):
        """В ящике тысяча, на счёте пусто — с банка снимать нечего."""
        resp = self._out(100, account=CashEntry.Account.BANK)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("подтвердите", str(resp.data).lower())

    def test_income_is_not_limited_by_the_balance(self):
        resp = self.client.post(self.URL, {
            "account": "CASH", "kind": "IN", "article": "DEPOSIT", "amount": "99999",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_future_date_is_refused(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        resp = self.client.post(self.URL, {
            "account": "CASH", "kind": "IN", "article": "DEPOSIT",
            "amount": "100", "happened_on": tomorrow,
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("будущем", str(resp.data).lower())

    def test_backdated_entry_is_still_allowed(self):
        """Задним числом кассу вносят постоянно — это не ошибка."""
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        resp = self.client.post(self.URL, {
            "account": "CASH", "kind": "IN", "article": "DEPOSIT",
            "amount": "100", "happened_on": yesterday,
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
