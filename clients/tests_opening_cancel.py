"""Отмена оплаты входящего долга (D-141, волна 3 к D-134).

Админ отменяет ОДНУ ошибочную оплату входящего долга: исходный приход остаётся
в кассовой книге, сегодня пишется встречный расход; запись оплаты помечается
отменённой (акт прошлого периода её помнит), остаток долга растёт. Акт сверки
и долг клиента после отмены сходятся; сверка ОПиУ→ОДДС — «Не объяснено» 0.
"""
from datetime import date
from decimal import Decimal

from django.utils import timezone

from audit.models import AuditLog
from clients.models import Client, OpeningBalance, OpeningDebtPayment
from clients.testkit import D
from clients.tests_opening import PHONES, OpeningCase, cash_total
from finance.models import CashEntry, ExpenseEntry, PeriodLock
from finance.reports import bridge as bridge_mod


class CancelOpeningPaymentTests(OpeningCase):
    PAID_ON = date(2026, 10, 2)

    def setUp(self):
        super().setUp()
        self.five_debts()
        self.c = Client.objects.get(phone=PHONES[0])
        self.ob = OpeningBalance.objects.get(client=self.c)

    def pay(self, amount, method="CASH", on=None):
        r = self.client.post(f"/api/clients/clients/{self.c.id}/pay-debt/", {
            "amount": str(amount), "method": method, "paid_on": (on or self.PAID_ON).isoformat(),
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return OpeningDebtPayment.objects.filter(opening=self.ob).order_by("-id").first()

    def cancel(self, payment, expect=200, **extra):
        r = self.client.post(f"/api/clients/opening-balances/{self.ob.id}/cancel-payment/",
                             {"payment": payment.id, **extra}, format="json")
        self.assertEqual(r.status_code, expect, getattr(r, "data", r))
        return r

    def debt(self):
        return Decimal(str(self.card(self.c)["debt"]))

    def test_cancel_one_payment_restores_debt_cash_and_statement(self):
        cash0 = cash_total()
        first = self.pay(5000)
        self.pay(3000, method="MBANK")
        self.assertEqual(self.debt(), D("4000"))
        self.assertEqual(cash_total() - cash0, D("8000"))

        r = self.cancel(first, reason="не тот клиент")
        self.assertEqual(Decimal(r.data["remaining"]), D("9000"))
        self.assertEqual(Decimal(r.data["paid"]), D("3000"))
        cancelled = [p for p in r.data["payments"] if p["cancelled"]]
        self.assertEqual([p["id"] for p in cancelled], [first.id])
        # Долг клиента и его остаток — 9 000; касса — минус 5 000 встречной записью.
        self.assertEqual(self.debt(), D("9000"))
        self.ob.refresh_from_db()
        self.assertEqual(self.ob.remaining, D("9000"))
        self.assertEqual(cash_total() - cash0, D("3000"))
        first.refresh_from_db()
        back = CashEntry.objects.get(pk=first.cancel_cash_entry_id)
        self.assertEqual((back.kind, back.article, back.amount, back.account),
                         ("OUT", CashEntry.Article.UNPAY, D("5000"), "CASH"))
        self.assertEqual(back.happened_on, timezone.localdate())
        self.assertTrue(CashEntry.objects.filter(pk=first.cash_entry_id).exists())  # приход остался
        self.assertTrue(AuditLog.objects.filter(action__contains="отмена оплаты входящего долга").exists())

        # Акт сверки: оплата — своим днём, отмена — дебетом сегодня; сальдо = долг.
        st = self.statement(self.c)
        kinds = [x["kind"] for x in st["rows"]]
        self.assertEqual(kinds.count("opening_payment"), 2)
        self.assertIn("opening_payment_cancelled", kinds)
        self.assertEqual(Decimal(st["closing"]), self.debt())
        # Акт на день оплаты не переписан: на 02.10 клиент был должен 4 000.
        st = self.statement(self.c, date_to=self.PAID_ON.isoformat())
        self.assertEqual(Decimal(st["closing"]), D("4000"))

        # Сверка ОПиУ→ОДДС за октябрь: «Не объяснено» 0, строка — 3 000.
        month = (date(2026, 10, 1), date(2026, 10, 31))
        b = bridge_mod.bridge(*month)
        self.assertEqual(b["unexplained"], D("0"))
        self.assertEqual({x["key"]: x["amount"] for x in b["lines"]}["opening_balances"], D("3000"))
        self.assertEqual(b["levels"]["receivables"], D("0"))

    def test_cancel_twice_and_foreign_payment(self):
        p = self.pay(2000)
        self.cancel(p)
        r = self.cancel(p, expect=400)
        self.assertIn("уже отменена", r.data["detail"])
        other = OpeningBalance.objects.get(client__phone=PHONES[1])
        r = self.client.post(f"/api/clients/opening-balances/{other.id}/cancel-payment/",
                             {"payment": p.id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_only_admin(self):
        p = self.pay(2000)
        for user in (self.store, self.acc):
            self.client.force_authenticate(user)
            r = self.client.post(f"/api/clients/opening-balances/{self.ob.id}/cancel-payment/",
                                 {"payment": p.id}, format="json")
            self.assertEqual(r.status_code, 403)
        p.refresh_from_db()
        self.assertIsNone(p.cancelled_at)

    def test_period_lock_by_today(self):
        p = self.pay(2000)
        PeriodLock.objects.update_or_create(pk=1, defaults={"closed_through": timezone.localdate()})
        r = self.cancel(p, expect=400)
        self.assertIn("период закрыт", str(r.data))
        p.refresh_from_db()
        self.assertIsNone(p.cancelled_at)

    def test_write_off_cancel_removes_bad_debt_expense(self):
        cash0 = cash_total()
        p = self.pay(12000, method="WRITE_OFF")
        self.assertEqual(self.debt(), D("0"))
        self.assertTrue(ExpenseEntry.objects.filter(pk=p.expense_id).exists())
        self.cancel(p)
        self.assertFalse(ExpenseEntry.objects.filter(pk=p.expense_id).exists())
        self.assertEqual(cash_total(), cash0)
        self.assertEqual(self.debt(), D("12000"))
        st = self.statement(self.c)
        self.assertIn("opening_write_off_cancelled", [x["kind"] for x in st["rows"]])
        self.assertEqual(Decimal(st["closing"]), D("12000"))
        month = (date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(bridge_mod.bridge(*month)["unexplained"], D("0"))

    def test_write_off_in_closed_month_cannot_be_cancelled(self):
        p = self.pay(1000, method="WRITE_OFF")
        PeriodLock.objects.update_or_create(pk=1, defaults={"closed_through": self.PAID_ON})
        self.cancel(p, expect=400)
        self.assertTrue(ExpenseEntry.objects.filter(pk=p.expense_id).exists())

    def test_balance_with_only_cancelled_payments_can_be_reverted(self):
        p = self.pay(4000)
        r = self.client.post(f"/api/clients/opening-balances/{self.ob.id}/revert/")
        self.assertEqual(r.status_code, 400)
        self.cancel(p)
        r = self.client.post(f"/api/clients/opening-balances/{self.ob.id}/revert/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.debt(), D("0"))
        self.assertEqual(self.statement(self.c)["rows"], [])
        month = (date(2026, 10, 1), date(2026, 10, 31))
        b = bridge_mod.bridge(*month)
        self.assertEqual(b["unexplained"], D("0"))
        self.assertEqual({x["key"]: x["amount"] for x in b["lines"]}["opening_balances"], D("0"))
