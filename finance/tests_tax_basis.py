"""Основа налога (PNL-14): «по начислению» (D-10, по умолчанию) или «по кассе».

Октябрь 2026: заказ 3 000 (10 шт. × 300), клиент принёс 1 000, остальные 2 000 —
долг; налог 4 %. В ноябре клиент отдал долг 2 000.
  по начислению: октябрь 4 % × 3 000 = 120, ноябрь 0;
  по кассе:      октябрь 4 % × 1 000 = 40,  ноябрь 4 % × 2 000 = 80.
Налог за два месяца одинаков (160 = 160), меняется месяц.
"""
from datetime import date
from decimal import Decimal

from audit.models import AuditLog
from finance.models import TaxRate
from finance.reports import bridge as bridge_mod
from finance.reports.pnl import pnl, pnl_year
from finance.tests_reports_calc import PnlCase

D = Decimal
OCT, OCT_END = date(2026, 10, 1), date(2026, 10, 31)
NOV, NOV_END = date(2026, 11, 1), date(2026, 11, 30)


class TaxBasisTests(PnlCase):
    def setUp(self):
        super().setUp()
        self.receipt = self.sale(date(2026, 10, 10), qty=10, paid="1000")

    def pay_debt_in_november(self):
        """Клиент отдал остаток 2 000 второго ноября (запись кассы, как её пишет оплата долга)."""
        from finance.models import CashEntry
        CashEntry.objects.create(
            account="CASH", kind="IN", article="SALE", amount=D("2000"),
            happened_on=date(2026, 11, 2), receipt=self.receipt, is_auto=True)

    def set_basis(self, basis):
        rate = TaxRate.objects.get(valid_from=OCT)
        r = self.client.patch(f"/api/finance/tax-rates/{rate.id}/", {"basis": basis}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def tax(self, first, last):
        return pnl(first, last)["tax"]

    def test_default_is_accrual(self):
        self.pay_debt_in_november()
        self.assertEqual(TaxRate.objects.get(valid_from=OCT).basis, "ACCRUAL")
        self.assertEqual((self.tax(OCT, OCT_END), self.tax(NOV, NOV_END)), (D("120.00"), D("0")))

    def test_cash_basis_follows_the_money(self):
        self.pay_debt_in_november()
        self.set_basis("CASH")
        self.assertEqual((self.tax(OCT, OCT_END), self.tax(NOV, NOV_END)), (D("40.00"), D("80.00")))
        self.assertEqual(self.tax(OCT, NOV_END), D("120.00"))            # итог тот же — 4 % от 3 000
        p = pnl(OCT, OCT_END)
        self.assertEqual(p["tax_basis"], "CASH")
        self.assertIn("полученных денег", p["tax_label"])

    def test_cash_basis_nets_refunds_and_change(self):
        """Деньги нетто: возврат клиенту уменьшает базу."""
        from finance.models import CashEntry

        self.pay_debt_in_november()
        CashEntry.objects.create(
            account="CASH", kind="OUT", article="REFUND", amount=D("500"),
            happened_on=date(2026, 11, 20), receipt=self.receipt, is_auto=True)
        self.set_basis("CASH")
        self.assertEqual(self.tax(NOV, NOV_END), D("60.00"))              # 4 % × (2 000 − 500)

    def test_bridge_stays_at_zero_on_cash_basis(self):
        self.pay_debt_in_november()
        self.set_basis("CASH")
        for first, last in ((OCT, OCT_END), (NOV, NOV_END), (OCT, NOV_END)):
            self.assertEqual(bridge_mod.bridge(first, last)["unexplained"], D("0"), (first, last))

    def test_year_table_label_and_change_is_audited(self):
        self.set_basis("CASH")
        row = next(r for r in pnl_year(2026)["rows"] if r["key"] == "tax")
        self.assertIn("полученных денег", row["label"])
        log = AuditLog.objects.filter(kind="tax").first()
        self.assertIn("основа", log.action)
        self.assertIn("→", log.action)

    def test_basis_change_is_blocked_in_a_closed_month(self):
        from finance.models import PeriodLock

        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 31)
        lock.save()
        rate = TaxRate.objects.get(valid_from=OCT)
        r = self.client.patch(f"/api/finance/tax-rates/{rate.id}/", {"basis": "CASH"}, format="json")
        self.assertEqual(r.status_code, 400)
