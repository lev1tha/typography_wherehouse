"""Этап 2 переделки ОПиУ/ОДДС (2026-10-07): формулы слоя `finance/reports`.

По тесту на каждую формулу — округление, амортизация, строки ОПиУ, налог,
ОДДС, сверка — и инварианты:
- прибыль «Сводки» = ОПиУ = «Обзор» = сумма графика по дням;
- остаток на начало + поток + вне потока = остаток на конец (и по счетам);
- перевод между счетами не меняет ни итог денег, ни поток;
- «Не объяснено» в сверке = 0.

Суммы в тестах посчитаны руками в комментариях.
"""
from datetime import date, datetime, time
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry, ExpenseEntry, ExpenseKind, TaxRate
from finance.reports import bridge as bridge_mod
from finance.reports import depreciation
from finance.reports.cashflow import cash_flow, cash_flow_year
from finance.reports.daily import daily_report
from finance.reports.money import cumulative_split, down2, pct, q2, split_evenly
from finance.reports.overview import headline, previous_period
from finance.reports.pnl import pnl, pnl_by_day
from finance.reports.summary import finance_summary
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material

D = Decimal


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


class MoneyRuleTests(APITestCase):
    def test_q2_is_half_up(self):
        self.assertEqual(q2(D("0.125")), D("0.13"))
        self.assertEqual(q2(D("0.124")), D("0.12"))
        self.assertEqual(down2(D("0.129")), D("0.12"))

    def test_pct(self):
        self.assertEqual(pct(D("1"), D("3")), D("33.3"))
        self.assertIsNone(pct(D("5"), D("0")))

    def test_split_evenly_sums_exactly(self):
        parts = split_evenly(D("100"), 3)
        self.assertEqual(parts, [D("33.33"), D("33.33"), D("33.34")])
        self.assertEqual(sum(parts), D("100"))
        tiny = split_evenly(D("0.03"), 6)
        self.assertEqual(sum(tiny), D("0.03"))
        self.assertTrue(all(p >= 0 for p in tiny))
        self.assertEqual(sum(split_evenly(D("-10"), 3)), D("-10"))
        self.assertEqual(split_evenly(D("5"), 0), [])

    def test_cumulative_split_matches_the_rounded_total(self):
        exact = [D("0.333"), D("0.333"), D("0.334"), D("1.005")]
        parts = cumulative_split(exact)
        self.assertEqual(sum(parts), q2(sum(exact)))


class DepreciationTests(APITestCase):
    def setUp(self):
        self.kind = ExpenseKind.objects.get(code=ExpenseKind.EQUIPMENT)

    def asset(self, amount, day, life=60, until=None):
        return ExpenseEntry.objects.create(
            kind=self.kind, amount=D(amount), spent_at=day, useful_life_months=life,
            depreciate_until=until,
        )

    def test_shares_sum_to_the_price_and_start_next_month(self):
        """100 000 на 60 мес.: 1 666,66 × 59 = 98 332,94, последний 1 667,06."""
        sched = depreciation.schedule(self.asset("100000", date(2026, 10, 20)))
        months = sorted(sched)
        self.assertEqual(months[0], date(2026, 11, 1))          # месяц покупки — без доли
        self.assertEqual(len(months), 60)
        self.assertEqual(sched[months[0]][0], D("1666.66"))
        self.assertEqual(sched[months[-1]][0], D("1667.06"))
        self.assertEqual(sum(r + d for r, d in sched.values()), D("100000"))

    def test_mid_month_purchase_is_the_same_as_first_day(self):
        a = depreciation.schedule(self.asset("1200", date(2026, 10, 1), life=12))
        b = depreciation.schedule(self.asset("1200", date(2026, 10, 31), life=12))
        self.assertEqual(a, b)

    def test_disposal_writes_off_the_rest_in_its_month(self):
        """1 200 на 12 мес. с ноября, выбыло в январе: ноябрь, декабрь, январь
        по 100 + списание 900 в январе — итого 1 200, дальше пусто."""
        sched = depreciation.schedule(self.asset("1200", date(2026, 10, 5), life=12,
                                                 until=date(2027, 1, 1)))
        self.assertEqual(sorted(sched), [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)])
        self.assertEqual(sched[date(2027, 1, 1)], (D("100.00"), D("900.00")))
        self.assertEqual(sum(r + d for r, d in sched.values()), D("1200"))

    def test_disposal_before_the_schedule_starts(self):
        sched = depreciation.schedule(self.asset("5000", date(2026, 10, 5), until=date(2026, 10, 1)))
        self.assertEqual(sched, {date(2026, 10, 1): (D("0"), D("5000"))})

    def test_disposal_after_the_end_changes_nothing(self):
        full = depreciation.schedule(self.asset("1200", date(2026, 10, 5), life=12))
        late = depreciation.schedule(self.asset("1200", date(2026, 10, 5), life=12, until=date(2030, 1, 1)))
        self.assertEqual(full, late)

    def test_by_month_aggregates_assets(self):
        self.asset("1200", date(2026, 10, 5), life=12)
        self.asset("600", date(2026, 11, 5), life=6)
        dec = depreciation.by_month(date(2026, 12, 1), date(2026, 12, 1))[date(2026, 12, 1)]
        self.assertEqual(dec["depreciation"], D("200.00"))      # 100 + 100


class PnlCase(APITestCase):
    """Общее: штучный материал по 300, себестоимость 100."""

    def setUp(self):
        self.admin = User.objects.create_user(username="pc_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.plate = Material.objects.create(
            name="Табличка", unit=Material.Unit.PIECE, quantity=D("1000"),
            purchase_price=D("100"), price_per_unit=D("300"),
        )

    def sale(self, day, qty=1, paid=None, method="CASH"):
        return sale_service.create_sale(
            client=None, cashier=self.admin, payment_method=method,
            items_data=[{"type": "MATERIAL", "material": self.plate,
                         "quantity": D(qty), "mode": "PIECE"}],
            amount_paid=D(paid) if paid is not None else None,
            created_at=noon(day),
        )

    def expense(self, code, amount, spent_at, period=None, account="CASH", **extra):
        payload = {"kind": ExpenseKind.objects.get(code=code).id, "amount": str(amount),
                   "spent_at": spent_at.isoformat(), "account": account, **extra}
        if period:
            payload["period"] = period
        r = self.client.post("/api/finance/expense-entries/", payload, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return ExpenseEntry.objects.get(pk=r.data["id"])

    def cash(self, article, kind, amount, day, account="CASH"):
        return CashEntry.objects.create(account=account, kind=kind, article=article,
                                        amount=D(amount), happened_on=day)


class PnlLinesTests(PnlCase):
    YEAR = 2025      # налога ещё нет: ставка с 10.2026

    def test_structure_adds_up(self):
        m = date(self.YEAR, 3, 1)
        self.sale(date(self.YEAR, 3, 5), qty=5)                         # 1 500 / 500
        self.expense("RENT", 250, date(self.YEAR, 3, 3))
        self.cash("COUNT", "OUT", 30, date(self.YEAR, 3, 20))           # недостача
        p = pnl(m, date(self.YEAR, 3, 31))
        self.assertEqual(p["revenue"], D("1500"))
        self.assertEqual(p["cogs_total"], D("500"))
        self.assertEqual(p["gross_profit"], D("1000"))
        self.assertEqual(p["opex"]["total"], D("250"))
        self.assertEqual(p["cash_count"], D("-30"))
        self.assertEqual(p["ebitda"], D("720"))                         # 1000 − 250 − 30
        self.assertEqual(p["net_profit"], D("720"))
        self.assertEqual(p["margins"]["gross"], D("66.7"))
        self.assertEqual(p["margins"]["net"], D("48.0"))

    def test_losses_sit_in_cost_of_sales(self):
        from warehouse.models import InventoryLog

        InventoryLog.objects.create(
            type=InventoryLog.Type.WRITE_OFF, material=self.plate, quantity_changed=D("-1"),
            cost=D("100"), happened_at=noon(date(self.YEAR, 3, 7)),
        )
        p = pnl(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        self.assertEqual(p["losses"], D("100"))
        self.assertEqual(p["gross_profit"], D("-100"))

    def test_expense_goes_to_its_accrual_month_cash_to_payment_day(self):
        """Аренда сентября, оплаченная 5 октября (D-2)."""
        self.expense("RENT", 25000, date(self.YEAR, 10, 5), period=f"{self.YEAR}-09")
        sep = pnl(date(self.YEAR, 9, 1), date(self.YEAR, 9, 30))
        octo = pnl(date(self.YEAR, 10, 1), date(self.YEAR, 10, 31))
        self.assertEqual(sep["opex"]["total"], D("25000"))
        self.assertEqual(octo["opex"]["total"], D("0"))
        self.assertEqual(cash_flow(date(self.YEAR, 9, 1), date(self.YEAR, 9, 30))["net_flow"], D("0"))
        self.assertEqual(cash_flow(date(self.YEAR, 10, 1), date(self.YEAR, 10, 31))["net_flow"], D("-25000"))

    def test_capex_is_depreciation_not_opex(self):
        self.expense("EQUIPMENT", 60000, date(self.YEAR, 3, 10))
        mar = pnl(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        apr = pnl(date(self.YEAR, 4, 1), date(self.YEAR, 4, 30))
        self.assertEqual((mar["opex"]["total"], mar["depreciation"]), (D("0"), D("0")))
        self.assertEqual(mar["capex_purchases"], D("60000"))
        self.assertEqual(apr["depreciation"], D("1000.00"))
        self.assertEqual(apr["ebitda"], D("0"))
        self.assertEqual(apr["operating_profit"], D("-1000.00"))

    def test_interest_is_below_operating_profit(self):
        self.expense("INTEREST", 700, date(self.YEAR, 3, 10))
        p = pnl(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        self.assertEqual(p["operating_profit"], D("0"))
        self.assertEqual(p["interest"], D("700"))
        self.assertEqual(p["net_profit"], D("-700"))

    def test_tax_payment_is_not_an_expense(self):
        self.expense("TAX", 900, date(self.YEAR, 3, 10))
        self.assertEqual(pnl(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))["net_profit"], D("0"))

    def test_old_cash_expense_counts_by_its_day(self):
        self.cash("EXPENSE", "OUT", 120, date(self.YEAR, 3, 4))
        p = pnl(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        self.assertEqual(p["opex_cash_manual"], D("120"))
        self.assertEqual(p["net_profit"], D("-120"))

    def test_cash_surplus_raises_profit(self):
        self.cash("COUNT", "IN", 50, date(self.YEAR, 3, 4))
        self.assertEqual(pnl(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))["ebitda"], D("50"))


class TaxTests(PnlCase):
    def test_no_tax_before_october_2026(self):
        self.sale(date(2026, 9, 10), qty=10)
        self.assertEqual(pnl(date(2026, 9, 1), date(2026, 9, 30))["tax"], D("0"))

    def test_four_percent_of_revenue(self):
        self.sale(date(2026, 11, 10), qty=10)                           # 3 000
        p = pnl(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(p["tax"], D("120.00"))
        self.assertEqual(p["tax_label"], "Налог (4 % от выручки)")
        self.assertEqual(p["net_profit"], D("3000") - D("1000") - D("120"))

    def test_refunds_reduce_the_base(self):
        receipt = self.sale(date(2026, 11, 10), qty=10)
        sale_service.refund_receipt(receipt, item_ids=[receipt.items.get().id], user=self.admin)
        type(receipt.items.get()).objects.filter(receipt=receipt).update(returned_at=noon(date(2026, 11, 20)))
        self.sale(date(2026, 11, 21), qty=5)                            # 1 500
        p = pnl(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(p["revenue"], D("1500"))
        self.assertEqual(p["tax"], D("60.00"))

    def test_rate_change_works_only_forward(self):
        self.sale(date(2026, 12, 10), qty=10)
        self.sale(date(2027, 1, 10), qty=10)
        TaxRate.objects.create(valid_from=date(2027, 1, 1), rate=D("3"))
        self.assertEqual(pnl(date(2026, 12, 1), date(2026, 12, 31))["tax"], D("120.00"))
        self.assertEqual(pnl(date(2027, 1, 1), date(2027, 1, 31))["tax"], D("90.00"))

    def test_daily_tax_parts_add_up_to_the_month(self):
        TaxRate.objects.create(valid_from=date(2027, 2, 1), rate=D("3.33"))
        for day in (3, 4, 9, 17):
            self.sale(date(2027, 2, day), qty=1)                        # 300 в день
        month = pnl(date(2027, 2, 1), date(2027, 2, 28))
        days = pnl_by_day(date(2027, 2, 1), date(2027, 2, 28))
        self.assertEqual(month["tax"], q2(D("1200") * D("3.33") / 100))  # 39.96
        self.assertEqual(sum(d["tax"] for d in days), month["tax"])


class OnlineRecognitionTests(PnlCase):
    def checkout_online(self):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "ONLINE",
            "items": [{"type": "MATERIAL", "material": self.plate.id, "quantity": 2, "mode": "PIECE"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def test_unpaid_online_order_is_neither_revenue_nor_debt(self):
        receipt = self.checkout_online()
        today = timezone.localdate()
        p = pnl(today.replace(day=1), today)
        self.assertEqual(p["revenue"], D("0"))
        self.assertEqual(receipt.debt, D("0"))
        summary = finance_summary(today.replace(day=1), today)
        self.assertEqual(summary["client_debt"], D("0"))

    def test_confirmed_online_order_brings_revenue_and_cost_together(self):
        receipt = self.checkout_online()
        sale_service.confirm_payment(receipt)
        today = timezone.localdate()
        p = pnl(today.replace(day=1), today)
        self.assertEqual(p["revenue"], D("600"))
        self.assertEqual(p["cogs_total"], D("200"))


class CashFlowTests(PnlCase):
    YEAR = 2025

    def test_transfer_changes_neither_total_nor_flow(self):
        self.cash("DEPOSIT", "IN", 5000, date(self.YEAR, 3, 1))
        before = cash_flow(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        self.cash("TRANSFER", "OUT", 1000, date(self.YEAR, 3, 10), account="CASH")
        self.cash("TRANSFER", "IN", 1000, date(self.YEAR, 3, 10), account="BANK")
        cf = cash_flow(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        self.assertEqual(cf["net_flow"], before["net_flow"])
        self.assertEqual(cf["closing"], before["closing"])
        self.assertEqual(cf["outside"], D("0"))
        self.assertEqual(cf["unmatched_transfers"], D("0"))
        self.assertEqual(cf["by_account"]["CASH"]["closing"], D("4000"))
        self.assertEqual(cf["by_account"]["BANK"]["closing"], D("1000"))
        self.assertTrue(cf["balanced"])

    def test_half_a_transfer_is_flagged_not_hidden(self):
        self.cash("TRANSFER", "OUT", 1000, date(self.YEAR, 3, 10))
        cf = cash_flow(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        self.assertEqual(cf["net_flow"], D("0"))
        self.assertEqual(cf["unmatched_transfers"], D("-1000"))
        self.assertTrue(cf["balanced"])
        year = cash_flow_year(self.YEAR)
        warn = next(r for r in year["rows"] if r["key"] == "unmatched_transfers")
        self.assertTrue(warn["warn"])

    def test_opening_balance_is_outside_the_flow(self):
        self.cash("OPENING", "IN", 150000, date(self.YEAR, 3, 1), account="BANK")
        cf = cash_flow(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        self.assertEqual(cf["net_flow"], D("0"))
        self.assertEqual(cf["outside"], D("150000"))
        self.assertEqual(cf["closing"], D("150000"))

    def test_each_movement_in_its_activity(self):
        self.sale(date(self.YEAR, 3, 5), qty=2, paid="600")
        self.expense("RENT", 250, date(self.YEAR, 3, 6))
        self.expense("EQUIPMENT", 30000, date(self.YEAR, 3, 7), account="BANK")
        self.expense("CUTTER", 5000, date(self.YEAR, 3, 7))
        self.expense("INTEREST", 300, date(self.YEAR, 3, 8))
        self.expense("TAX", 200, date(self.YEAR, 3, 8))
        self.cash("LOAN_IN", "IN", 40000, date(self.YEAR, 3, 1), account="BANK")
        self.cash("UNPAY", "OUT", 100, date(self.YEAR, 3, 9))
        cf = cash_flow(date(self.YEAR, 3, 1), date(self.YEAR, 3, 31))
        lines = {(line["section"], line["key"]): line["amount"]
                 for s in cf["sections"].values() for line in s["lines"]}
        self.assertEqual(lines[("operating", "clients")], D("600"))
        self.assertEqual(lines[("operating", "unpay")], D("-100"))
        self.assertEqual(lines[("operating", "interest_paid")], D("-300"))
        self.assertEqual(lines[("operating", "tax_paid")], D("-200"))
        equipment = ExpenseKind.objects.get(code="EQUIPMENT")
        self.assertEqual(lines[("investing", f"kind:{equipment.id}")], D("-30000"))
        self.assertEqual(lines[("financing", "loan_in")], D("40000"))
        self.assertEqual(cf["sections"]["operating"]["total"], D("600") - 250 - 5000 - 300 - 200 - 100)
        self.assertEqual(cf["net_flow"], D("600") - 250 - 5000 - 300 - 200 - 100 - 30000 + 40000)

    def test_opening_plus_flow_plus_outside_is_closing_every_month(self):
        self.cash("OPENING", "IN", 1000, date(self.YEAR, 1, 1))
        self.sale(date(self.YEAR, 2, 5), qty=2, paid="600")
        self.cash("TRANSFER", "OUT", 300, date(self.YEAR, 3, 31), account="CASH")
        self.cash("TRANSFER", "IN", 300, date(self.YEAR, 4, 1), account="BANK")
        year = cash_flow_year(self.YEAR)
        rows = {r["key"]: r for r in year["rows"]}
        for i in range(12):
            outside = rows.get("section:outside", {"values": [D("0")] * 12})["values"][i]
            self.assertEqual(
                rows["opening"]["values"][i] + rows["net"]["values"][i] + outside,
                rows["closing"]["values"][i],
            )
            self.assertEqual(
                rows["closing:CASH"]["values"][i] + rows["closing:BANK"]["values"][i],
                rows["closing"]["values"][i],
            )
        self.assertTrue(year["balanced"])


class BridgeTests(PnlCase):
    """Ноябрь 2026, налог 4 %. Посчитано руками:

    продажа А 5.11: 3 шт. = 900 (себест. 300), принесли 500 → долг 400;
    продажа Б 6.11: 1 шт. = 300 (себест. 100), принесли 400 → сдача 100;
    приход 10 шт. × 100 в долг; станок 30 000 с банка; займ 50 000 на банк;
    аренда ноября 250 оплачена 2.12.

    Прибыль ноября = 1 200 − 400 − 250 − 48 (налог) = 502.
    Поток ноября = 500 + 400 − 30 000 + 50 000 = 20 900.
    """

    def setUp(self):
        super().setUp()
        self.sale(date(2026, 11, 5), qty=3, paid="500")
        self.sale(date(2026, 11, 6), qty=1, paid="400")
        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": self.plate.id, "quantity": "10", "actual_price": "100",
            "received_on": "2026-11-07",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        from warehouse.models import InventoryLog

        InventoryLog.objects.filter(type=InventoryLog.Type.SUPPLY).update(
            happened_at=noon(date(2026, 11, 7))
        )
        self.expense("EQUIPMENT", 30000, date(2026, 11, 10), account="BANK")
        self.cash("LOAN_IN", "IN", 50000, date(2026, 11, 1), account="BANK")
        self.expense("RENT", 250, date(2026, 12, 2), period="2026-11")

    def lines(self, first, last):
        b = bridge_mod.bridge(first, last)
        return {line["key"]: line["amount"] for line in b["lines"]}, b

    def test_november_by_hand(self):
        lines, b = self.lines(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(lines["net_profit"], D("502.00"))
        self.assertEqual(lines["receivables"], D("-400"))
        self.assertEqual(lines["client_money"], D("100"))
        self.assertEqual(lines["inventory"], D("-600"))            # 1 000 пришло − 400 продано
        self.assertEqual(lines["payables"], D("1000"))
        self.assertEqual(lines["accrued"], D("250"))
        self.assertEqual(lines["tax_payable"], D("48.00"))
        self.assertEqual(lines["capex"], D("-30000"))
        self.assertEqual(lines["financing"], D("50000"))
        self.assertEqual(b["net_cash_flow"], D("20900"))
        self.assertEqual(b["unexplained"], D("0"))

    def test_december_by_hand(self):
        """Декабрь: амортизация 500, аренда ноября оплачена, денег −250."""
        lines, b = self.lines(date(2026, 12, 1), date(2026, 12, 31))
        self.assertEqual(lines["net_profit"], D("-500.00"))
        self.assertEqual(lines["non_cash"], D("500.00"))
        self.assertEqual(lines["accrued"], D("-250"))
        self.assertEqual(b["net_cash_flow"], D("-250"))
        self.assertEqual(b["unexplained"], D("0"))

    def test_debt_paid_later_moves_receivables_back(self):
        receipt = Receipt.objects.order_by("created_at").first()
        sale_service.apply_payment(receipt, D("400"), user=self.admin, paid_on=date(2026, 12, 15))
        lines, b = self.lines(date(2026, 12, 1), date(2026, 12, 31))
        self.assertEqual(lines["receivables"], D("400"))
        self.assertEqual(b["unexplained"], D("0"))

    def test_lines_always_add_up_to_the_cash_flow(self):
        for first, last in ((date(2026, 11, 1), date(2026, 12, 31)), (date(2026, 11, 10), date(2026, 11, 20))):
            lines, b = self.lines(first, last)
            self.assertEqual(sum(v for k, v in lines.items()), b["net_cash_flow"])
            self.assertEqual(b["unexplained"], D("0"))


class InvariantTests(PnlCase):
    """Одна прибыль на всех экранах — месяц со всем сразу."""

    def setUp(self):
        super().setUp()
        self.month = date(2026, 11, 1)
        self.end = date(2026, 11, 30)
        self.sale(date(2026, 11, 3), qty=7, paid="1000")
        self.sale(date(2026, 11, 17), qty=1)
        self.expense("RENT", 30001, date(2026, 11, 2))                  # поровну по дням
        self.expense("CUTTER", 777, date(2026, 11, 9))                  # своим днём
        self.expense("TRANSPORT", 500, date(2026, 12, 3), period="2026-11")  # оплачен позже
        self.expense("INTEREST", 333, date(2026, 11, 25))
        ExpenseEntry.objects.create(kind=ExpenseKind.objects.get(code="EQUIPMENT"),
                                    amount=D("45000"), spent_at=date(2026, 8, 14))
        self.cash("COUNT", "OUT", 13, date(2026, 11, 28))

    def test_summary_pnl_overview_and_daily_agree(self):
        p = pnl(self.month, self.end)
        summary = finance_summary(self.month, self.end)
        head = headline(self.month, self.end)
        daily = daily_report(2026, 11)
        self.assertEqual(summary["profit"], p["net_profit"])
        self.assertEqual(head["net_profit"]["value"], p["net_profit"])
        self.assertEqual(daily["totals"]["profit"], p["net_profit"])
        self.assertEqual(summary["gross_margin"], p["gross_profit"])
        self.assertEqual(summary["total_expenses"], p["opex"]["total"] + p["opex_cash_manual"])
        # Рука: 2 400 − 800 − 30 001 − 777 − 500 − 13 − 750 (аморт.) − 333 − 96 (налог).
        self.assertEqual(p["net_profit"], D("2400") - 800 - 30001 - 777 - 500 - 13 - 750 - 333 - 96)

    def test_year_table_months_equal_the_period_report(self):
        from finance.reports.pnl import pnl_year

        year = pnl_year(2026)
        net = next(r for r in year["rows"] if r["key"] == "net")
        self.assertEqual(net["values"][10], pnl(self.month, self.end)["net_profit"])
        for r in year["rows"]:
            if r["kind"] != "percent":
                self.assertEqual(r["total"], sum(r["values"], D("0")), r["key"])

    def test_any_range_is_the_sum_of_its_days(self):
        first, last = date(2026, 11, 10), date(2026, 11, 20)
        days = pnl_by_day(first, last)
        self.assertEqual(sum(d["net_profit"] for d in days), pnl(first, last)["net_profit"])


class OverviewTests(PnlCase):
    def test_previous_period(self):
        self.assertEqual(previous_period(date(2026, 11, 1), date(2026, 11, 30)),
                         (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(previous_period(date(2026, 11, 10), date(2026, 11, 19)),
                         (date(2026, 10, 31), date(2026, 11, 9)))
        self.assertIsNone(previous_period(None, None))

    def test_tiles_compare_with_last_month_and_received_is_cash(self):
        self.sale(date(2026, 10, 10), qty=2, paid="600")
        self.sale(date(2026, 11, 10), qty=4, paid="300")                 # 1 200, принесли 300
        head = headline(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(head["revenue"]["value"], D("1200"))
        self.assertEqual(head["revenue"]["change"]["before"], D("600"))
        self.assertEqual(head["revenue"]["change"]["delta"], D("600"))
        self.assertEqual(head["revenue"]["change"]["delta_pct"], D("100.0"))
        self.assertEqual(head["received"]["total"], D("300"))
        self.assertEqual(head["cash_end"]["value"], D("900"))
        self.assertEqual(head["currency"], "сом")
        self.assertEqual(head["why"]["unexplained"], D("0"))
        self.assertTrue(head["why"]["reasons"])

    def test_dashboard_api_carries_the_headline(self):
        self.sale(date(2026, 11, 10), qty=1)
        r = self.client.get("/api/audit/dashboard/", {"date_from": "2026-11-01", "date_to": "2026-11-30"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(D(str(r.data["headline"]["revenue"]["value"])), D("300"))
        self.assertEqual(D(str(r.data["breakdown"]["profit_before_expenses"])),
                         D(str(self.client.get("/api/finance/report/", {
                             "date_from": "2026-11-01", "date_to": "2026-11-30"}).data["gross_margin"])))


class EndpointTests(PnlCase):
    def test_bridge_endpoint(self):
        r = self.client.get("/api/finance/bridge/", {"year": 2026})
        self.assertEqual(r.status_code, 200, r.data)
        keys = [row["key"] for row in r.data["rows"]]
        self.assertEqual(keys[0], "net_profit")
        self.assertEqual(keys[-1], "net_cash_flow")
        self.assertIn("unexplained", keys)
        self.assertEqual(self.client.get("/api/finance/bridge/", {"year": "x"}).status_code, 400)


class AccruedBasisFilterTests(PnlCase):
    def test_kind_dialog_can_list_by_accrual_month(self):
        """Аренда сентября, оплаченная в октябре: по «за какой месяц» — в
        сентябре (как строка «Сводки»), по дате оплаты — в октябре."""
        entry = self.expense("RENT", 25000, date(2025, 10, 5), period="2025-09")
        url = "/api/finance/expense-entries/"
        sep = {"date_from": "2025-09-01", "date_to": "2025-09-30"}
        by_period = self.client.get(url, {**sep, "basis": "accrued"}).data
        by_paid = self.client.get(url, sep).data
        self.assertEqual([r["id"] for r in by_period], [entry.id])
        self.assertEqual(by_paid, [])
