"""Решение владельца 11.10 (D-196): при переходе основы налога «по кассе» →
«по начислению» долг, оставшийся неоплаченным в «кассовом» месяце, НЕ
облагается — ни в том месяце (денег не было), ни позже, когда его оплатят
(«по начислению» облагается выручка месяца, а не деньги).

Зеркало D-163 (обратный переход «по начислению» → «по кассе»: оплата уже
обложенного долга второй раз не облагается). Это и было поведение системы —
тест его закрепляет.

Сценарий (2025, все даты в прошлом): 4 % по кассе в октябре, 4 % по
начислению с ноября.
  - 25.10 заказ на 3 000 в долг; оплачен 10.11;
  - 28.10 заказ на 1 500, оплачен сразу;
  - 03.11 заказ на 2 100 в долг (не оплачен).
Октябрь: 4 % × 1 500 = 60 (по кассе: в октябре пришли только эти деньги).
Ноябрь: 4 % × 2 100 = 84 (по начислению: выручка ноября). 3 000 октябрьского
долга не облагаются нигде.
"""
from datetime import date
from decimal import Decimal as D

from clients.models import Client
from finance.models import TaxRate
from finance.reports.bridge import bridge
from finance.reports.pnl import pnl
from finance.tests_reports_calc import PnlCase, noon
from sales import sale_service

OCT, OCT_END = date(2025, 10, 1), date(2025, 10, 31)
NOV, NOV_END = date(2025, 11, 1), date(2025, 11, 30)


class CashToAccrualTests(PnlCase):
    def setUp(self):
        super().setUp()
        TaxRate.objects.create(valid_from=OCT, rate=D("4"), basis=TaxRate.Basis.CASH)
        TaxRate.objects.create(valid_from=NOV, rate=D("4"), basis=TaxRate.Basis.ACCRUAL)
        self.client_ = Client.objects.create(full_name="Должник", phone="+996700000011")

    def order(self, day, qty, paid):
        return sale_service.create_sale(
            client=self.client_, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.plate, "quantity": D(qty), "mode": "PIECE"}],
            amount_paid=D(paid), created_at=noon(day),
        )

    def test_debt_left_in_a_cash_month_is_never_taxed(self):
        october_debt = self.order(date(2025, 10, 25), 10, "0")          # 3 000 в долг
        self.order(date(2025, 10, 28), 5, "1500")                        # 1 500 деньгами
        self.order(date(2025, 11, 3), 7, "0")                            # 2 100 в долг
        self.assertEqual(pnl(OCT, OCT_END)["tax"], D("60.00"))
        self.assertEqual(pnl(NOV, NOV_END)["tax"], D("84.00"))
        # Оплата октябрьского долга в ноябре налог ноября не меняет.
        sale_service.apply_payment(october_debt, None, user=self.admin, paid_on=date(2025, 11, 10), method="CASH")
        self.assertEqual(pnl(OCT, OCT_END)["tax"], D("60.00"))
        self.assertEqual(pnl(NOV, NOV_END)["tax"], D("84.00"))
        self.assertEqual(pnl(OCT, NOV_END)["tax"], D("144.00"))
        for first, last in ((OCT, OCT_END), (NOV, NOV_END), (OCT, NOV_END)):
            self.assertEqual(bridge(first, last)["unexplained"], D("0"), (first, last))

    def test_unpaid_debt_stays_untaxed(self):
        self.order(date(2025, 10, 25), 10, "0")
        self.assertEqual(pnl(OCT, NOV_END)["tax"], D("0"))
