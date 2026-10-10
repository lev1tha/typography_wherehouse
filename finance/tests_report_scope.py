"""Справочные таблицы отчёта читаются один раз, а не на каждый месяц.

Годовая таблица ОПиУ — 12 вызовов `pnl()`; каждый заново читал ставки налога,
траты, амортизируемые покупки, а ОДДС — всю кассовую книгу ради остатков.
Числа при этом обязаны остаться теми же, а между двумя отдельными вызовами
правки данных видны сразу (область отчёта живёт ровно один вызов).
"""
import re
from collections import Counter
from datetime import date
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from finance.models import CashEntry, ExpenseEntry, TaxRate
from finance.reports.bridge import bridge, bridge_year
from finance.reports.cashflow import cash_flow, cash_flow_year
from finance.reports.pnl import pnl, pnl_year, tax_rate_for
from finance.reports.scope import once, report_scope
from finance.tests_reports_calc import PnlCase

D = Decimal


def tables(ctx):
    found = Counter()
    for q in ctx.captured_queries:
        m = re.search(r'FROM "(\w+)"', q["sql"])
        found[m.group(1) if m else "?"] += 1
    return found


class ScopeTests(PnlCase):
    def setUp(self):
        super().setUp()
        for m in (9, 10, 11):
            self.sale(date(2026, m, 3), qty=3, paid="900")
        self.expense("RENT", 25000, date(2026, 10, 5), period="2026-09")
        self.expense("EQUIPMENT", 60000, date(2026, 10, 10))
        self.cash("LOAN_IN", "IN", 5000, date(2026, 11, 2))

    def test_once_outside_a_scope_always_reloads(self):
        calls = []
        once("k", lambda: calls.append(1))
        once("k", lambda: calls.append(1))
        self.assertEqual(len(calls), 2)

    def test_once_inside_a_scope_loads_once(self):
        calls = []

        @report_scope
        def report():
            once("k", lambda: calls.append(1))
            once("k", lambda: calls.append(1))

        report()
        self.assertEqual(len(calls), 1)
        report()
        self.assertEqual(len(calls), 2)       # новая область — новая загрузка

    def test_edits_between_calls_are_visible(self):
        before = pnl(date(2026, 9, 1), date(2026, 9, 30))["opex"]["total"]
        entry = ExpenseEntry.objects.get(period=date(2026, 9, 1))
        entry.amount = D("30000")
        entry.save()
        after = pnl(date(2026, 9, 1), date(2026, 9, 30))["opex"]["total"]
        self.assertEqual((before, after), (D("25000"), D("30000")))
        TaxRate.objects.create(valid_from=date(2026, 11, 1), rate=D("3"))
        self.assertEqual(tax_rate_for(date(2026, 11, 15)), D("3"))
        self.assertEqual(TaxRate.rate_for(date(2026, 11, 15)), tax_rate_for(date(2026, 11, 15)))

    def test_year_numbers_equal_month_by_month(self):
        from finance.periods import month_end

        year = pnl_year(2026)
        for key, field in (("revenue", "revenue"), ("net", "net_profit"), ("tax", "tax")):
            row = next(r for r in year["rows"] if r["key"] == key)
            for m in range(12):
                first = date(2026, m + 1, 1)
                single = pnl(first, month_end(first))[field]
                expected = -single if key == "tax" else single
                self.assertEqual(row["values"][m], expected, f"{key} {m + 1}")

    def test_reference_tables_are_not_read_per_month(self):
        with CaptureQueriesContext(connection) as ctx:
            pnl_year(2026)
        used = tables(ctx)
        self.assertLessEqual(used["finance_taxrate"], 1)
        self.assertLessEqual(used["finance_expenseentry"], 2)

        with CaptureQueriesContext(connection) as ctx:
            cash_flow_year(2026)
        self.assertLessEqual(tables(ctx)["finance_cashentry"], 13)      # 12 месяцев + книга

        with CaptureQueriesContext(connection) as ctx:
            bridge_year(2026)
        used = tables(ctx)
        self.assertLessEqual(used["finance_taxrate"], 1)
        self.assertLessEqual(used["finance_expenseentry"], 2)

    def test_balances_equal_the_cash_book(self):
        cf = cash_flow(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(cf["closing"], CashEntry.balance(upto=date(2026, 11, 30)))
        self.assertEqual(cf["opening"], CashEntry.balance(upto=date(2026, 10, 31)))
        self.assertTrue(cf["balanced"])

    def test_bridge_year_months_equal_single_bridges(self):
        year = bridge_year(2026)
        for row in year["rows"]:
            if row["key"] in ("net_profit", "receivables", "unexplained"):
                for m in (9, 10, 11):
                    first = date(2026, m, 1)
                    from finance.periods import month_end

                    single = bridge(first, month_end(first))
                    line = next(l for l in single["lines"] if l["key"] == row["key"])
                    self.assertEqual(row["values"][m - 1], line["amount"], f"{row['key']} {m}")
