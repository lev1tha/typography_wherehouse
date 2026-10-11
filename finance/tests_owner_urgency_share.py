"""Решение владельца 11.10 (D-197): СРОЧНОСТЬ НЕ ВХОДИТ В ПРОЦЕНТ МАСТЕРА.

Выработка для ведомости (`payroll.output`) и всё, что считает процент мастера
(«что если», доля мастера в марже строки и в «Сводке»), берут сумму строки без
наценки за срочность: сумма строки / (1 + срочность / 100), до тыйына. Метры
не меняются; выручка — как в чеке.

Резка 650 со срочностью 25 %: в чеке 813 (812,50 вверх до сома), в выработке
813 / 1,25 = 650,40 — сорок тыйынов — доля округления строки вверх.
"""
from datetime import date
from decimal import Decimal as D

from finance import payroll
from finance.reports.summary import finance_summary
from finance.reports.whatif import master_pay
from finance.tests_payroll import OCT, PayrollCase
from sales.models import TransactionItem
from services.models import PricingSettings

OCT_END = date(2026, 10, 31)


class UrgencyIsNotMasterWorkTests(PayrollCase):
    def setUp(self):
        super().setUp()
        receipt = self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 10, 100)    # 1 000, 10 пог.м
        self.urgent = TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.SERVICE, service=self.cnc,
            quantity=D("1"), price_per_item=D("813"), catalog_price=D("650"),
            urgency_percent=D("25"),
        )
        self.assertEqual(self.urgent.sold_total, D("813"))

    def test_output_takes_the_line_without_urgency(self):
        self.assertEqual(payroll.work_amount(self.urgent), D("650.40"))
        slot = payroll.output(OCT, OCT_END)[self.azamat.id]["CUTTING_CNC"]
        self.assertEqual(slot["amount"], D("1650.40"))
        self.assertEqual(slot["meters"], D("11"))                     # метры не меняются

    def test_statement_percent(self):
        row = self.row(payroll.statement(OCT), self.azamat)
        cut = next(line for line in row["lines"] if line["work"] == "CUTTING_CNC")
        self.assertEqual(cut["base"], D("1650.40"))
        self.assertEqual(row["percent_total"], D("99.02"))             # 6 % × 1 650,40

    def test_line_margin_share(self):
        self.assertEqual(payroll.line_master_share(self.urgent), D("39.02"))   # 6 % × 650,40

    def test_what_if_base(self):
        known_pay, known_base, unknown_base = master_pay(OCT, OCT_END)
        self.assertEqual(known_base, D("1650.40"))
        self.assertEqual(unknown_base, D("0"))

    def test_summary_master_share_but_revenue_with_urgency(self):
        settings = PricingSettings.load()
        settings.master_commission_percent = D("4")
        settings.save()
        cutting = finance_summary(OCT, OCT_END)["cutting"]
        self.assertEqual(cutting["total"], D("1813"))                  # выручка — как в чеке
        self.assertEqual(cutting["master_share"], D("66"))              # 4 % × 1 650,40
        row = next(r for r in cutting["by_user"] if r["kind"] == "user")
        self.assertEqual(row["amount"], D("1813"))
        self.assertEqual(row["master_share"], D("66"))

    def test_line_without_urgency_is_unchanged(self):
        plain = self.urgent.receipt.items.get(price_per_item=D("100"))
        self.assertEqual(payroll.work_amount(plain), D("1000"))
