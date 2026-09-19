"""Трата из «Финансов» уходит в кассовую книгу расходом.

Проверка прод-данных 19.09.2026: касса показывала 245 453 прихода и НИ ОДНОЙ
выплаты, хотя трат было внесено на 176 877 — зарплаты, аренда, коммуналка.
Вопрос «сколько сейчас в ящике», ради которого книга и заводилась, получал
ответ, завышенный на всю эту сумму.

Исключение одно: «долг материала». Эта запись означает «материал взяли, деньги
ещё не отдали» — расхода по ней не было, и писать его нельзя.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry, ExpenseEntry, ExpenseKind

CASH = CashEntry.Account.CASH
BANK = CashEntry.Account.BANK


class ExpenseGoesToCashBookTests(APITestCase):
    URL = "/api/finance/expense-entries/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ec_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.rent = ExpenseKind.objects.get(code="RENT")
        self.salary = ExpenseKind.objects.get(code=ExpenseKind.SALARY)
        self.debt = ExpenseKind.objects.get(code=ExpenseKind.MATERIAL_DEBT)

    def _add(self, kind, amount, **extra):
        payload = {
            "kind": kind.id, "name": "проверка", "amount": amount,
            "spent_at": "2026-09-10", **extra,
        }
        r = self.client.post(self.URL, payload, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_expense_leaves_the_drawer(self):
        self._add(self.rent, "5000")
        entry = CashEntry.objects.get()
        self.assertEqual(entry.kind, CashEntry.Kind.OUT)
        self.assertEqual(entry.article, CashEntry.Article.EXPENSE)
        self.assertEqual(entry.account, CASH)
        self.assertEqual(entry.amount, Decimal("5000"))
        # Дата расхода — та, которой трату записали, а не день ввода.
        self.assertEqual(entry.happened_on.isoformat(), "2026-09-10")
        self.assertEqual(CashEntry.balance(CASH), Decimal("-5000"))

    def test_bank_payment_does_not_touch_the_drawer(self):
        self._add(self.rent, "5000", account="BANK")
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))
        self.assertEqual(CashEntry.balance(BANK), Decimal("-5000"))

    def test_salary_keeps_its_own_article(self):
        self._add(self.salary, "12000")
        self.assertEqual(CashEntry.objects.get().article, CashEntry.Article.SALARY)

    def test_material_debt_is_not_money_out(self):
        """«Материал взяли в долг» — деньги ещё в ящике."""
        self._add(self.debt, "90000")
        self.assertFalse(CashEntry.objects.exists())
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_editing_the_expense_moves_the_cash_row(self):
        row = self._add(self.rent, "5000")
        r = self.client.patch(
            f"{self.URL}{row['id']}/", {"amount": "6000", "account": "BANK"}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        # Запись остаётся ОДНА: правка двигает её, а не добавляет вторую.
        entry = CashEntry.objects.get()
        self.assertEqual(entry.amount, Decimal("6000"))
        self.assertEqual(entry.account, BANK)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_deleting_the_expense_takes_the_cash_row_with_it(self):
        row = self._add(self.rent, "5000")
        r = self.client.delete(f"{self.URL}{row['id']}/")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(CashEntry.objects.exists())
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_cash_row_of_an_expense_is_not_edited_by_hand(self):
        """Иначе касса разошлась бы с отчётом, и объяснить это было бы нечем."""
        self._add(self.rent, "5000")
        entry = CashEntry.objects.get()
        r = self.client.patch(
            f"/api/finance/cash/{entry.id}/", {"amount": "1"}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("трате", r.data["detail"])

    def test_report_and_cash_book_tell_the_same_story(self):
        """Расходы отчёта и выплаты кассы — одни и те же деньги."""
        self._add(self.rent, "25000")
        self._add(self.salary, "58491")
        self._add(self.debt, "90000")   # в кассу не идёт
        report = self.client.get("/api/finance/report/").data
        out = -CashEntry.balance(CASH)
        self.assertEqual(Decimal(str(report["total_expenses"])), Decimal("83491"))
        self.assertEqual(out, Decimal("83491"))
        # Запись долга материала при этом в отчёте видна — справочной строкой.
        self.assertEqual(ExpenseEntry.objects.filter(kind=self.debt).count(), 1)
