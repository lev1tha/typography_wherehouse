"""Замок периода при правке сумм/дат и проверка суммы траты.

1. PATCH ручной записи кассы не проверял замок: приход 5 000 от 10.09 при
   закрытом по 30.09 периоде менялся на 9 000 или уезжал в октябрь — ОДДС
   сентября «5 000 → 9 000 → 0».
2. PATCH суммы траты проверял замок «за какой месяц» только при смене самого
   месяца: аренда «за сентябрь» 25 000 → 40 000 при закрытом сентябре.
3. Трата −5 000 и 0 принималась.
"""
from datetime import date

from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry, ExpenseEntry, ExpenseKind, PeriodLock
from finance.reports.cashflow import cash_flow


class LockCase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="lk_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def close_through(self, day):
        r = self.client.patch("/api/finance/period/", {"closed_through": day.isoformat()}, format="json")
        self.assertEqual(r.status_code, 200, r.data)


class CashEntryEditLockTests(LockCase):
    def setUp(self):
        super().setUp()
        self.entry = CashEntry.objects.create(
            account="CASH", kind="IN", article=CashEntry.Article.OTHER, amount=5000,
            happened_on=date(2026, 9, 10), is_auto=False,
        )
        self.close_through(date(2026, 9, 30))
        self.url = f"/api/finance/cash/{self.entry.pk}/"

    def test_amount_of_closed_entry_cannot_change(self):
        r = self.client.patch(self.url, {"amount": "9000"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("период закрыт", str(r.data))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.amount, 5000)

    def test_closed_entry_cannot_be_moved_to_open_month(self):
        r = self.client.patch(self.url, {"happened_on": "2026-10-05"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.happened_on, date(2026, 9, 10))

    def test_open_entry_cannot_be_moved_into_closed_month(self):
        open_entry = CashEntry.objects.create(
            account="CASH", kind="IN", article=CashEntry.Article.OTHER, amount=700,
            happened_on=date(2026, 10, 3), is_auto=False,
        )
        r = self.client.patch(f"/api/finance/cash/{open_entry.pk}/",
                              {"happened_on": "2026-09-20"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_open_entry_is_still_editable(self):
        open_entry = CashEntry.objects.create(
            account="CASH", kind="IN", article=CashEntry.Article.OTHER, amount=700,
            happened_on=date(2026, 10, 3), is_auto=False,
        )
        r = self.client.patch(f"/api/finance/cash/{open_entry.pk}/", {"amount": "800"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_september_flow_does_not_move(self):
        before = cash_flow(date(2026, 9, 1), date(2026, 9, 30))["net_flow"]
        self.client.patch(self.url, {"amount": "9000"}, format="json")
        self.client.patch(self.url, {"happened_on": "2026-10-05"}, format="json")
        self.assertEqual(cash_flow(date(2026, 9, 1), date(2026, 9, 30))["net_flow"], before)

    def test_unlocking_lets_the_edit_through(self):
        PeriodLock.objects.update(closed_through=None)
        r = self.client.patch(self.url, {"amount": "9000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)


class ExpenseEditLockTests(LockCase):
    def setUp(self):
        super().setUp()
        self.rent = ExpenseKind.objects.get(code="RENT")
        r = self.client.post("/api/finance/expense-entries/", {
            "kind": self.rent.id, "amount": "25000", "spent_at": "2026-10-05",
            "period": "2026-09", "account": "CASH",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.entry = ExpenseEntry.objects.get(pk=r.data["id"])
        self.close_through(date(2026, 9, 30))        # сентябрь закрыт, октябрь открыт
        self.url = f"/api/finance/expense-entries/{self.entry.pk}/"

    def test_amount_of_closed_accrual_month_cannot_change(self):
        r = self.client.patch(self.url, {"amount": "40000"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("период закрыт", str(r.data))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.amount, 25000)

    def test_kind_and_account_of_closed_month_cannot_change(self):
        for body in ({"account": "BANK"}, {"kind": ExpenseKind.objects.get(code="UTILITIES").id}):
            with self.subTest(body=body):
                r = self.client.patch(self.url, body, format="json")
                self.assertEqual(r.status_code, 400, r.data)

    def test_note_of_closed_month_can_still_be_edited(self):
        """Примечание на отчёты не влияет — его правка замка не касается."""
        r = self.client.patch(self.url, {"note": "уточнили"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_moving_into_closed_month_is_refused(self):
        other = ExpenseEntry.objects.create(
            kind=self.rent, amount=100, spent_at=date(2026, 10, 7), period=date(2026, 10, 1)
        )
        r = self.client.patch(f"/api/finance/expense-entries/{other.pk}/",
                              {"period": "2026-09"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_open_month_expense_is_still_editable(self):
        other = ExpenseEntry.objects.create(
            kind=self.rent, amount=100, spent_at=date(2026, 10, 7), period=date(2026, 10, 1)
        )
        r = self.client.patch(f"/api/finance/expense-entries/{other.pk}/",
                              {"amount": "150"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)


class ExpenseAmountTests(LockCase):
    URL = "/api/finance/expense-entries/"

    def post(self, amount):
        return self.client.post(self.URL, {
            "kind": ExpenseKind.objects.get(code="RENT").id, "amount": amount,
            "spent_at": "2026-10-05", "account": "CASH",
        }, format="json")

    def test_zero_and_negative_are_refused(self):
        for amount in ("-5000", "0", "0.00"):
            with self.subTest(amount=amount):
                r = self.post(amount)
                self.assertEqual(r.status_code, 400, r.data)
                self.assertIn("больше нуля", str(r.data["amount"]))
        self.assertEqual(ExpenseEntry.objects.count(), 0)

    def test_positive_is_accepted(self):
        self.assertEqual(self.post("0.01").status_code, 201)

    def test_patch_to_negative_is_refused(self):
        entry = ExpenseEntry.objects.create(
            kind=ExpenseKind.objects.get(code="RENT"), amount=100, spent_at=date(2026, 10, 7)
        )
        r = self.client.patch(f"{self.URL}{entry.pk}/", {"amount": "-1"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_legacy_negative_entry_stays_editable_by_other_fields(self):
        legacy = ExpenseEntry.objects.create(
            kind=ExpenseKind.objects.get(code="RENT"), amount=-300, spent_at=date(2026, 10, 7)
        )
        r = self.client.patch(f"{self.URL}{legacy.pk}/", {"note": "сторно"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
