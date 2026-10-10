"""Повторяющиеся траты, «что если», кварталы и выгрузка периода (PNL-09, G2-N1/-N4)."""
from datetime import date
from decimal import Decimal

from audit.models import AuditLog
from finance import recurring
from finance.models import CashEntry, ExpenseEntry, ExpenseKind, PeriodLock, RecurringExpense
from finance.reports.bridge import bridge_year
from finance.reports.cashflow import cash_flow_year
from finance.reports.pnl import pnl, pnl_year
from finance.reports.whatif import what_if_year
from finance.tests_reports_calc import PnlCase
from services.models import PrintingService
from sales.models import TransactionItem

D = Decimal


class RecurringTests(PnlCase):
    def rule(self, **extra):
        data = dict(kind=ExpenseKind.objects.get(code="RENT"), name="Аренда цеха", amount=D("25000"),
                    day=10, start_month=date(2026, 8, 1), created_by=self.admin)
        data.update(extra)
        return RecurringExpense.objects.create(**data)

    def test_creates_one_expense_per_month_whose_day_has_come(self):
        self.rule()
        result = recurring.generate(today=date(2026, 10, 12), user=self.admin)
        self.assertEqual([c["month"] for c in result["created"]],
                         [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)])
        e = ExpenseEntry.objects.get(period=date(2026, 9, 1))
        self.assertEqual((e.spent_at, e.amount, e.kind.code), (date(2026, 9, 10), D("25000"), "RENT"))
        self.assertEqual(CashEntry.objects.filter(article="EXPENSE").count(), 3)    # и в кассе

    def test_future_day_is_not_created_and_rerun_does_not_duplicate(self):
        self.rule()
        recurring.generate(today=date(2026, 10, 9), user=self.admin)        # 10 октября ещё не наступило
        self.assertEqual(ExpenseEntry.objects.count(), 2)
        recurring.generate(today=date(2026, 10, 9), user=self.admin)
        self.assertEqual(ExpenseEntry.objects.count(), 2)
        recurring.generate(today=date(2026, 10, 10), user=self.admin)
        self.assertEqual(ExpenseEntry.objects.count(), 3)
        recurring.generate(today=date(2026, 10, 31), user=self.admin)
        self.assertEqual(ExpenseEntry.objects.count(), 3)

    def test_until_month_stops_the_schedule(self):
        self.rule(until_month=date(2026, 9, 1))
        recurring.generate(today=date(2026, 12, 20), user=self.admin)
        self.assertEqual(sorted(ExpenseEntry.objects.values_list("period", flat=True)),
                         [date(2026, 8, 1), date(2026, 9, 1)])

    def test_short_month_uses_the_last_day(self):
        self.rule(day=31, start_month=date(2026, 2, 1))
        recurring.generate(today=date(2026, 3, 1), user=self.admin)
        self.assertEqual(ExpenseEntry.objects.get(period=date(2026, 2, 1)).spent_at, date(2026, 2, 28))

    def test_closed_months_are_skipped_not_failed(self):
        self.rule()
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 9, 30)
        lock.save()
        result = recurring.generate(today=date(2026, 10, 12), user=self.admin)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertEqual(ExpenseEntry.objects.get().period, date(2026, 10, 1))

    def test_inactive_rule_creates_nothing(self):
        self.rule(is_active=False)
        self.assertEqual(recurring.generate(today=date(2026, 10, 12))["created"], [])

    def test_pnl_picks_up_the_generated_rent(self):
        self.rule(start_month=date(2026, 10, 1))
        recurring.generate(today=date(2026, 10, 12), user=self.admin)
        self.assertEqual(pnl(date(2026, 10, 1), date(2026, 10, 31))["opex"]["total"], D("25000"))

    def test_api_crud_validation_and_run(self):
        rent = ExpenseKind.objects.get(code="RENT").id
        body = {"kind": rent, "name": "Аренда", "amount": "25000", "day": 10, "start_month": "2026-10"}
        r = self.client.post("/api/finance/recurring/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        bad_kind = ExpenseKind.objects.get(code=ExpenseKind.EQUIPMENT).id
        self.assertEqual(self.client.post("/api/finance/recurring/", {**body, "kind": bad_kind}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/finance/recurring/", {**body, "day": 32}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/finance/recurring/", {**body, "until_month": "2026-09"}, format="json").status_code, 400)
        run = self.client.post("/api/finance/recurring/run/", {}, format="json")
        self.assertEqual(run.status_code, 200)
        self.assertGreaterEqual(run.data["created"], 0)
        self.assertTrue(AuditLog.objects.filter(kind="expense", action__contains="Повторяющаяся").exists())


class WhatIfTests(PnlCase):
    """Октябрь: материал 10 × 300 = 3 000 (закуп 1 000), резка 2 000 (себестоимость расходников 400)."""

    def setUp(self):
        super().setUp()
        self.cnc = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC)
        receipt = self.sale(date(2026, 10, 5), qty=10, paid="3000")
        TransactionItem.objects.create(receipt=receipt, type="SERVICE", service=self.cnc,
                                       quantity=D("20"), price_per_item=D("100"), cost_total=D("400"))
        from django.db.models import F
        from sales.models import Receipt
        Receipt.objects.filter(pk=receipt.pk).update(total_price=F("total_price") + 2000, amount_paid=F("amount_paid") + 2000)

    def test_zero_scenario_equals_base(self):
        data = what_if_year(2026, 0, 0)
        for row in data["rows"]:
            self.assertEqual(row["base"], row["scenario"], row["key"])
            self.assertEqual(row["delta_total"], D("0"))

    def test_service_price_up_ten_percent(self):
        """+10 % к услугам: выручка +200, налог 4 % от 200 = +8, чистая прибыль +192."""
        data = what_if_year(2026, 10, 0)
        rows = {r["key"]: r for r in data["rows"]}
        self.assertEqual(rows["revenue"]["delta_total"], D("200.00"))
        self.assertEqual(rows["tax"]["delta_total"], D("8.00"))
        self.assertEqual(rows["net"]["delta_total"], D("192.00"))
        self.assertEqual(rows["cogs"]["delta_total"], D("0"))
        self.assertEqual(rows["revenue"]["delta"][9], D("200.00"))          # октябрь

    def test_purchase_up_twenty_percent(self):
        """+20 % к закупу: себестоимость (1 000 + 400) × 20 % = +280, прибыль −280."""
        rows = {r["key"]: r for r in what_if_year(2026, 0, 20)["rows"]}
        self.assertEqual(rows["cogs"]["delta_total"], D("280.00"))
        self.assertEqual(rows["gross"]["delta_total"], D("-280.00"))
        self.assertEqual(rows["net"]["delta_total"], D("-280.00"))

    def test_nothing_is_written(self):
        before = (ExpenseEntry.objects.count(), CashEntry.objects.count(), TransactionItem.objects.count())
        what_if_year(2026, 15, 15)
        self.assertEqual(before, (ExpenseEntry.objects.count(), CashEntry.objects.count(),
                                  TransactionItem.objects.count()))

    def test_api_validates_numbers(self):
        url = "/api/finance/pnl/what-if/"
        self.assertEqual(self.client.get(url, {"year": 2026, "price_pct": "5", "cost_pct": "-3"}).status_code, 200)
        self.assertEqual(self.client.get(url, {"year": 2026, "price_pct": "NaN"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"year": 2026, "price_pct": "abc"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"year": 2026, "price_pct": "5000"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"year": 2026, "price_pct": "5,5"}).status_code, 200)


class QuarterTests(PnlCase):
    def test_quarters_are_sums_of_three_months_and_total_is_the_year(self):
        self.sale(date(2026, 10, 5), qty=10, paid="3000")      # 3 000
        self.sale(date(2026, 11, 5), qty=5, paid="1500")       # 1 500
        self.sale(date(2026, 12, 5), qty=2, paid="600")        # 600
        data = pnl_year(2026)
        revenue = next(r for r in data["rows"] if r["key"] == "revenue")
        self.assertEqual(revenue["quarters"], [D("0"), D("0"), D("0"), D("5100")])
        self.assertEqual(sum(revenue["quarters"]), revenue["total"])
        net = next(r for r in data["rows"] if r["key"] == "net")
        self.assertEqual(sum(net["quarters"]), net["total"])
        pct_row = next(r for r in data["rows"] if r["key"] == "net_pct")
        self.assertIsNone(pct_row["quarters"][0])                        # нет выручки — нечего делить
        self.assertIsNotNone(pct_row["quarters"][3])
        self.assertEqual([q["quarter"] for q in data["quarters"]], [1, 2, 3, 4])

    def test_cash_flow_balances_use_first_and_last_month(self):
        CashEntry.objects.create(kind="IN", article="DEPOSIT", amount=D("1000"), happened_on=date(2026, 2, 5))
        CashEntry.objects.create(kind="OUT", article="OTHER", amount=D("300"), happened_on=date(2026, 5, 5))
        data = cash_flow_year(2026)
        rows = {r["key"]: r for r in data["rows"]}
        self.assertEqual(rows["opening"]["quarters"], [D("0"), D("1000"), D("700"), D("700")])
        self.assertEqual(rows["closing"]["quarters"], [D("1000"), D("700"), D("700"), D("700")])
        self.assertEqual(rows["closing:CASH"]["quarters"][0], D("1000"))
        net = rows["net"]
        self.assertEqual(sum(net["quarters"]), net["total"])

    def test_bridge_rows_have_quarters(self):
        data = bridge_year(2026)
        self.assertTrue(all(len(r["quarters"]) == 4 for r in data["rows"]))
        unexplained = next(r for r in data["rows"] if r["key"] == "unexplained")
        self.assertEqual(unexplained["quarters"], [D("0")] * 4)


class PeriodExportTests(PnlCase):
    def setUp(self):
        super().setUp()
        self.sale(date(2026, 10, 5), qty=10, paid="3000")                  # наличные 3 000
        self.sale(date(2026, 10, 6), qty=5, paid="1500", method="MBANK")   # безнал 1 500
        self.expense("RENT", "1000", date(2026, 10, 10))

    def get(self, **params):
        r = self.client.get("/api/finance/export/period/", params)
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        return r.content.decode("utf-8-sig").splitlines()

    def test_quarter_export_has_pnl_cash_and_bridge_with_split(self):
        lines = self.get(year=2026, quarter=4)
        self.assertEqual(lines[0], "Отчёт за период;01.10.2026;31.12.2026")
        self.assertIn("Основа налога;по начислению (от выручки)", lines)
        self.assertIn("Выручка;4500,00", lines)
        self.assertIn("  выручка наличными;3000,00", lines)
        self.assertIn("  выручка безналом;1500,00", lines)
        self.assertTrue(any(l.startswith("ОДДС;Наличные;Безнал;Всего") for l in lines))
        self.assertTrue(any(l.startswith("Остаток на конец;") for l in lines))
        self.assertTrue(any(l.startswith("Не объяснено;0,00") for l in lines))

    def test_export_matches_the_screen_numbers(self):
        p = pnl(date(2026, 10, 1), date(2026, 12, 31))
        lines = self.get(date_from="2026-10-01", date_to="2026-12-31")
        net = next(l for l in lines if l.startswith("Чистая прибыль;"))
        self.assertEqual(net, "Чистая прибыль;" + format(p["net_profit"], "f").replace(".", ","))

    def test_tax_basis_is_named_in_the_file(self):
        from finance.models import TaxRate
        TaxRate.objects.filter(valid_from=date(2026, 10, 1)).update(basis="CASH")
        lines = self.get(year=2026, quarter=4)
        self.assertIn("Основа налога;по кассе (от полученных денег)", lines)

    def test_bad_params_are_400(self):
        self.assertEqual(self.client.get("/api/finance/export/period/", {"year": "abc"}).status_code, 400)
        self.assertEqual(self.client.get("/api/finance/export/period/",
                                         {"date_from": "2026-12-01", "date_to": "2026-10-01"}).status_code, 400)
