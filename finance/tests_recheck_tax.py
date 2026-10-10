"""Перепроверка владельца 10.10, RF-N6 (S2): переход основы налога с «начисления»
на «кассу» облагал второй раз оплату долга, выручка которого уже обложена.

Сценарий (2д в owner_pnl_recheck.py, перенесён в 2025, чтобы все даты были в
прошлом): 4 % по начислению в октябре, 4 % по кассе с ноября.
  - заказ 25.10 на 3 000 в долг, долг оплачен 10.11;
  - аванс 10 000 другого клиента 05.11;
  - входящий долг 5 000 (на 30.09) оплачен 20.11.
Excel: октябрь 4 % × 3 000 = 120 (по начислению); ноябрь 4 % × (10 000 + 5 000)
= 600 — оплата октябрьского долга уже обложена в октябре. Система давала 720.
"""
from datetime import date

from clients import advances as adv_mod
from clients import opening as opening_mod
from clients.models import Client
from finance.models import CashEntry, TaxRate
from finance.reports import bridge as bridge_mod
from finance.reports.export import period_rows
from finance.reports.pnl import pnl
from finance.tests_reports_calc import PnlCase, noon
from sales import sale_service

from decimal import Decimal as D

SEP, SEP_END = date(2025, 9, 1), date(2025, 9, 30)
OCT, OCT_END = date(2025, 10, 1), date(2025, 10, 31)
NOV, NOV_END = date(2025, 11, 1), date(2025, 11, 30)


class BasisSwitchTests(PnlCase):
    def setUp(self):
        super().setUp()
        TaxRate.objects.create(valid_from=OCT, rate=D("4"), basis=TaxRate.Basis.ACCRUAL)
        TaxRate.objects.create(valid_from=NOV, rate=D("4"), basis=TaxRate.Basis.CASH)
        self.c1 = Client.objects.create(full_name="Долг октября", phone="+996700000001")
        self.c2 = Client.objects.create(full_name="Авансист", phone="+996700000002")

    def debt_sale(self, day, client=None, qty=10):
        return sale_service.create_sale(
            client=client or self.c1, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.plate, "quantity": D(qty), "mode": "PIECE"}],
            amount_paid=D("0"), created_at=noon(day),
        )

    def scenario(self):
        self.october = self.debt_sale(date(2025, 10, 25))                     # 3 000 в долг
        sale_service.apply_payment(self.october, None, user=self.admin, paid_on=date(2025, 11, 10), method="CASH")
        adv_mod.accept_advance(self.c2, D("10000"), method="CASH", paid_on=date(2025, 11, 5), user=self.admin)
        opening_mod.post("+996700000003;Старый должник;5000", date(2025, 9, 30), user=self.admin)
        c3 = Client.objects.get(phone__endswith="700000003")
        opening_mod.pay_opening_debts(c3, D("5000"), user=self.admin, paid_on=date(2025, 11, 20), method="CASH")

    def test_debt_taxed_on_accrual_is_not_taxed_again_on_cash(self):
        self.scenario()
        self.assertEqual(pnl(OCT, OCT_END)["tax"], D("120.00"))
        self.assertEqual(pnl(NOV, NOV_END)["tax"], D("600.00"))
        self.assertEqual(pnl(OCT, NOV_END)["tax"], D("720.00"))
        for first, last in ((OCT, OCT_END), (NOV, NOV_END), (OCT, NOV_END)):
            self.assertEqual(bridge_mod.bridge(first, last)["unexplained"], D("0"), (first, last))

    def test_overpaid_change_of_that_debt_is_left_out_too(self):
        """Принесли больше долга, сдачу отдали — ни приход, ни сдача по нему в базу не идут."""
        receipt = self.debt_sale(date(2025, 10, 25))
        CashEntry.objects.create(account="CASH", kind="IN", article="SALE", amount=D("5000"),
                                 happened_on=date(2025, 11, 10), receipt=receipt, is_auto=True)
        CashEntry.objects.create(account="CASH", kind="OUT", article="CHANGE", amount=D("2000"),
                                 happened_on=date(2025, 11, 10), receipt=receipt, is_auto=True)
        self.assertEqual(pnl(NOV, NOV_END)["tax"], D("0"))

    def test_refund_of_an_accrual_month_order_still_lowers_the_cash_base(self):
        """Возврат — поправка к уже обложенной выручке: уменьшает базу по кассе, как раньше."""
        receipt = self.debt_sale(date(2025, 10, 25))
        sale_service.apply_payment(receipt, None, user=self.admin, paid_on=date(2025, 10, 26), method="CASH")
        self.debt_sale(date(2025, 11, 3), qty=20)                              # 6 000 в долг, денег нет
        CashEntry.objects.create(account="CASH", kind="IN", article="SALE", amount=D("6000"),
                                 happened_on=date(2025, 11, 4), receipt=None, is_auto=True)
        CashEntry.objects.create(account="CASH", kind="OUT", article="REFUND", amount=D("1000"),
                                 happened_on=date(2025, 11, 12), receipt=receipt, is_auto=True)
        self.assertEqual(pnl(NOV, NOV_END)["tax"], D("200.00"))                 # 4 % × (6 000 − 1 000)

    def test_order_of_a_month_without_tax_is_taxed_when_paid(self):
        """Сентябрь без ставки: выручка не облагалась — по кассе её оплата облагается."""
        old = self.debt_sale(date(2025, 9, 20))
        sale_service.apply_payment(old, None, user=self.admin, paid_on=date(2025, 11, 10), method="CASH")
        self.assertEqual(pnl(SEP, SEP_END)["tax"], D("0"))
        self.assertEqual(pnl(NOV, NOV_END)["tax"], D("120.00"))

    def test_order_of_a_cash_month_paid_later_is_taxed(self):
        nov = self.debt_sale(date(2025, 11, 3))
        sale_service.apply_payment(nov, None, user=self.admin, paid_on=date(2025, 11, 15), method="CASH")
        self.assertEqual(pnl(NOV, NOV_END)["tax"], D("120.00"))

    def labels(self, first, last):
        return {row[0]: row[1:] for row in period_rows(first, last) if row}

    def test_export_says_mixed_basis(self):
        self.scenario()
        both = self.labels(OCT, NOV_END)
        self.assertIn("смешанная основа", str(both["Основа налога"][0]))
        nov = self.labels(NOV, NOV_END)
        self.assertIn("смешанная основа", str(nov["Основа налога"][0]))
        excluded = next(v for k, v in nov.items() if str(k).startswith("Не облагается по кассе"))
        self.assertEqual(D(str(excluded[0])), D("3000.00"))

    def test_export_of_a_plain_month_is_unchanged(self):
        self.scenario()
        oct_ = self.labels(OCT, OCT_END)
        self.assertNotIn("смешанная", str(oct_["Основа налога"][0]))
        self.assertFalse(any(str(k).startswith("Не облагается по кассе") for k in oct_))
