"""Кассовая книга как лист Excel (cash-05, G1-N1): остаток после операции, поиск и
фильтры, итоги дня, CSV, пересчёт на дату, отметка «сверено с выпиской»."""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from finance.models import CashEntry, ExpenseEntry, ExpenseKind, PeriodLock

D = Decimal
URL = "/api/finance/cash/"


def entry(day, kind, amount, account="CASH", article="OTHER", **extra):
    return CashEntry.objects.create(
        happened_on=day, kind=kind, amount=D(amount), account=account, article=article, **extra)


class CashBookCase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="boss", password="x", role=User.Role.ADMIN)
        self.nurbek = User.objects.create_user(username="nurbek", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        self.today = timezone.localdate()


class RunningBalanceTests(CashBookCase):
    def test_balance_after_each_operation_per_account(self):
        a = entry(date(2026, 10, 1), "IN", "1000", article="DEPOSIT")
        b = entry(date(2026, 10, 2), "OUT", "300")
        c = entry(date(2026, 10, 2), "IN", "5000", account="BANK", article="DEPOSIT")
        d = entry(date(2026, 10, 3), "OUT", "200")
        rows = {r["id"]: r for r in self.client.get(URL).data["results"]}
        self.assertEqual(rows[a.id]["balance_after"], D("1000"))
        self.assertEqual(rows[b.id]["balance_after"], D("700"))
        self.assertEqual(rows[c.id]["balance_after"], D("5000"))         # у банка свой остаток
        self.assertEqual(rows[d.id]["balance_after"], D("500"))

    def test_balance_after_ignores_the_filter(self):
        entry(date(2026, 10, 1), "IN", "1000", article="DEPOSIT")
        small = entry(date(2026, 10, 2), "OUT", "300")
        rows = self.client.get(URL, {"amount": "300"}).data["results"]
        self.assertEqual([(r["id"], r["balance_after"]) for r in rows], [(small.id, D("700"))])

    def test_same_day_order_follows_entry_time(self):
        first = entry(date(2026, 10, 1), "IN", "100", article="DEPOSIT")
        second = entry(date(2026, 10, 1), "OUT", "40")
        rows = {r["id"]: r["balance_after"] for r in self.client.get(URL).data["results"]}
        self.assertEqual((rows[first.id], rows[second.id]), (D("100"), D("60")))


class FilterTests(CashBookCase):
    def setUp(self):
        super().setUp()
        self.e1 = entry(date(2026, 10, 1), "IN", "4200", article="DEPOSIT", note="Тахир принёс", created_by=self.nurbek)
        self.e2 = entry(date(2026, 10, 2), "OUT", "1500", note="инкассация", created_by=self.admin)
        self.e3 = entry(date(2026, 10, 3), "OUT", "300", note="такси", created_by=self.admin, reconciled=True)

    def ids(self, **params):
        return {r["id"] for r in self.client.get(URL, params).data["results"]}

    def test_amount_exact_and_range(self):
        self.assertEqual(self.ids(amount="4200"), {self.e1.id})
        self.assertEqual(self.ids(amount_min="1000"), {self.e1.id, self.e2.id})
        self.assertEqual(self.ids(amount_min="300", amount_max="1500"), {self.e2.id, self.e3.id})

    def test_cashier_and_note_search(self):
        self.assertEqual(self.ids(created_by=self.nurbek.id), {self.e1.id})
        self.assertEqual(self.ids(search="инкасс"), {self.e2.id})
        self.assertEqual(self.ids(search="nurbek"), {self.e1.id})
        self.assertEqual(self.ids(search="нет такого"), set())

    def test_order_and_client_search(self):
        from clients.models import Client
        from sales import sale_service
        from warehouse.models import Material

        plate = Material.objects.create(name="Табличка", unit=Material.Unit.PIECE, quantity=D("100"),
                                        purchase_price=D("100"), price_per_unit=D("300"))
        client = Client.objects.create(full_name="Тахир Абдыкеримов", phone="+996555111222")
        receipt = sale_service.create_sale(
            client=client, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": plate, "quantity": D("1"), "mode": "PIECE"}],
            amount_paid=D("300"))
        paid = CashEntry.objects.get(receipt=receipt)
        self.assertEqual(self.ids(order=receipt.order_number), {paid.id})
        self.assertIn(paid.id, self.ids(search=str(receipt.order_number)))
        self.assertEqual(self.ids(search="Абдыкер"), {paid.id})
        self.assertEqual(self.ids(search="555111"), {paid.id})
        row = next(r for r in self.client.get(URL, {"order": receipt.order_number}).data["results"])
        self.assertEqual(row["client_name"], "Тахир Абдыкеримов")

    def test_reconciled_filter_and_marking(self):
        self.assertEqual(self.ids(reconciled="true"), {self.e3.id})
        r = self.client.post(URL + "reconcile/", {"ids": [self.e1.id, self.e2.id], "reconciled": True}, format="json")
        self.assertEqual((r.status_code, r.data["updated"]), (200, 2))
        self.assertEqual(self.ids(reconciled="false"), set())
        self.client.post(URL + "reconcile/", {"ids": [self.e1.id], "reconciled": False}, format="json")
        self.e1.refresh_from_db()
        self.assertFalse(self.e1.reconciled)
        self.assertIsNone(self.e1.reconciled_at)

    def test_reconcile_is_for_the_admin(self):
        acc = User.objects.create_user(username="acc", password="x", role=User.Role.ACCOUNTANT)
        self.client.force_authenticate(acc)
        r = self.client.post(URL + "reconcile/", {"ids": [self.e1.id]}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_reconciling_does_not_touch_a_closed_month(self):
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 31)
        lock.save()
        r = self.client.post(URL + "reconcile/", {"ids": [self.e1.id], "reconciled": True}, format="json")
        self.assertEqual(r.status_code, 200)


class DayTotalsAndCsvTests(CashBookCase):
    def test_day_totals_carry_the_closing_balance(self):
        entry(date(2026, 9, 30), "IN", "1000", article="DEPOSIT")
        entry(date(2026, 10, 1), "OUT", "300")
        entry(date(2026, 10, 1), "IN", "100", article="DEPOSIT")
        entry(date(2026, 10, 3), "OUT", "50", account="BANK")
        rows = self.client.get(URL + "day-totals/", {"date_from": "2026-10-01", "date_to": "2026-10-31"}).data["results"]
        self.assertEqual([r["date"] for r in rows], [date(2026, 10, 3), date(2026, 10, 1)])
        day1 = rows[1]
        self.assertEqual((day1["income"], day1["outcome"], day1["net"]), (D("100"), D("300"), D("-200")))
        self.assertEqual(day1["closing"]["CASH"], D("800"))               # 1 000 − 300 + 100
        self.assertEqual(day1["closing_total"], D("800"))
        self.assertEqual(rows[0]["closing"], {"CASH": D("800"), "BANK": D("-50")})

    def test_csv_has_balance_after_and_signed_amounts(self):
        entry(date(2026, 10, 1), "IN", "1000", article="DEPOSIT", note="взнос")
        entry(date(2026, 10, 2), "OUT", "300", note="такси", created_by=self.nurbek)
        r = self.client.get(URL + "export/")
        self.assertEqual(r.status_code, 200)
        text = r.content.decode("utf-8-sig").splitlines()
        self.assertTrue(text[0].startswith("Дата;Счёт;Тип;Статья;Сумма;Остаток счёта после"))
        self.assertIn("02.10.2026;Наличные;Расход;Прочее;-300,00;700,00;;;nurbek;такси;нет", text)
        self.assertIn("01.10.2026;Наличные;Приход;Вложение владельца;1000,00;1000,00;;;система;взнос;нет", text)


class CountTests(CashBookCase):
    def count(self, **body):
        body.setdefault("counted", "1000")
        return self.client.post(URL + "count/", body, format="json")

    def test_count_on_a_past_date_stays_in_that_day(self):
        entry(date(2026, 10, 1), "IN", "1000", article="DEPOSIT")
        entry(self.today, "IN", "500", article="DEPOSIT")                  # позже даты пересчёта
        day = self.today - timedelta(days=3)
        r = self.count(counted="987.50", happened_on=day.isoformat())
        self.assertEqual(r.status_code, 201, r.data)
        made = CashEntry.objects.get(article="COUNT")
        self.assertEqual((made.happened_on, made.kind, made.amount), (day, "OUT", D("12.50")))

    def test_nan_infinity_negative_and_huge_are_refused_not_500(self):
        for bad in ("NaN", "Infinity", "-Infinity", "-100", "1e29", "10000000000000000"):
            r = self.count(counted=bad)
            self.assertEqual(r.status_code, 400, (bad, r.data))
        self.assertEqual(self.count(counted="abc").status_code, 400)
        self.assertFalse(CashEntry.objects.exists())

    def test_future_date_and_closed_period_are_refused(self):
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual(self.count(happened_on=tomorrow).status_code, 400)
        self.assertEqual(self.count(happened_on="not-a-date").status_code, 400)
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 9, 30)
        lock.save()
        self.assertEqual(self.count(happened_on="2026-09-15").status_code, 400)

    def test_default_day_is_today_and_match_writes_nothing(self):
        entry(self.today, "IN", "1000", article="DEPOSIT")
        r = self.count()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["diff"], "0")
        self.assertEqual(CashEntry.objects.count(), 1)


class OtherWarningAndAuditTests(CashBookCase):
    def post(self, **body):
        data = {"account": "CASH", "kind": "OUT", "article": "OTHER", "amount": "100",
                "confirm_negative": True, **body}
        return self.client.post(URL, data, format="json")

    def test_manual_out_other_warns_that_it_is_not_in_the_pnl(self):
        r = self.post()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["warnings"][0]["code"], "other_not_in_pnl")
        self.assertIn("ОПиУ", r.data["warnings"][0]["message"])

    def test_other_warnings_only_for_manual_out_other(self):
        self.assertNotIn("warnings", self.post(article="DEPOSIT", kind="IN").data)
        self.assertNotIn("warnings", self.post(kind="IN").data)
        self.assertNotIn("warnings", self.post(article="TRANSFER").data)

    def test_manual_cash_entry_edit_and_delete_leave_a_trail(self):
        made = self.post(note="такси").data
        self.client.patch(f"{URL}{made['id']}/", {"amount": "250", "confirm_negative": True}, format="json")
        self.client.delete(f"{URL}{made['id']}/")
        texts = list(AuditLog.objects.filter(kind="cash").order_by("id").values_list("action", flat=True))
        self.assertTrue(any("100" in t and "такси" in t for t in texts))
        self.assertTrue(any("сумма 100 → 250" in t for t in texts), texts)
        self.assertTrue(any("удалена" in t for t in texts))


class DuplicateExpenseTests(CashBookCase):
    URL = "/api/finance/expense-entries/"

    def setUp(self):
        super().setUp()
        self.rent = ExpenseKind.objects.get(code="RENT")

    def post(self, **extra):
        body = {"kind": self.rent.id, "name": "аренда", "amount": "25000", "spent_at": "2026-10-10", **extra}
        return self.client.post(self.URL, body, format="json")

    def test_same_kind_amount_and_date_asks_for_confirmation(self):
        self.assertEqual(self.post().status_code, 201)
        r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertIn("confirm_duplicate", r.data)
        self.assertEqual(ExpenseEntry.objects.count(), 1)
        self.assertEqual(self.post(confirm_duplicate=True).status_code, 201)
        self.assertEqual(ExpenseEntry.objects.count(), 2)

    def test_different_amount_or_date_is_not_a_duplicate(self):
        self.post()
        self.assertEqual(self.post(amount="25001").status_code, 201)
        self.assertEqual(self.post(spent_at="2026-10-11").status_code, 201)

    def test_expense_edit_is_audited_was_to_became(self):
        made = self.post().data
        self.client.patch(f"{self.URL}{made['id']}/", {"amount": "27000", "note": "индексация"}, format="json")
        log = AuditLog.objects.filter(kind="expense", action__contains="изменена").first()
        self.assertIn("сумма 25 000 → 27 000", log.action)
        self.assertIn("примечание — → индексация", log.action)
        self.client.delete(f"{self.URL}{made['id']}/")
        self.assertTrue(AuditLog.objects.filter(kind="expense", action__contains="удалена").exists())


class SettingsAuditTests(CashBookCase):
    def test_threshold_change_is_in_the_journal(self):
        r = self.client.patch("/api/finance/settings/", {"capitalization_threshold": "30000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        log = AuditLog.objects.filter(kind="settings").first()
        self.assertIn("порог капвложения 20 000 → 30 000", log.action)

    def test_untouched_settings_leave_no_record(self):
        self.client.patch("/api/finance/settings/", {"capitalization_threshold": "20000"}, format="json")
        self.assertFalse(AuditLog.objects.filter(kind="settings").exists())

    def test_payroll_day_is_validated(self):
        r = self.client.patch("/api/finance/settings/", {"payroll_prev_month_until_day": 40}, format="json")
        self.assertEqual(r.status_code, 400)
