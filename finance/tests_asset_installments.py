"""Актив в рассрочку (PNL-03): карточка на полную цену, платежи — только деньги.

Станок 120 000 куплен 25.10.2026: 40 000 заплатили сразу (банк), 80 000 — 25.11.
Раньше каждый платёж был отдельной покупкой: 40 000 амортизировались как
станок за 40 000 (с ноября), 80 000 — другим станком (с декабря), а ниже порога
платёж уходил расходом месяца. Теперь:
  карточка 120 000 на 60 мес. → амортизация 2 000,00 в месяц с 11.2026;
  платежи 40 000 (октябрь) и 80 000 (ноябрь) — ОДДС, инвестиционная деятельность;
  в ОПиУ платежей нет; «Не объяснено» = 0.
"""
from datetime import date
from decimal import Decimal

from finance.models import CashEntry, ExpenseEntry, ExpenseKind
from finance.reports import bridge as bridge_mod
from finance.reports.cashflow import cash_flow
from finance.reports.pnl import pnl
from finance.reports.summary import finance_summary
from finance.tests_reports_calc import PnlCase

D = Decimal
URL = "/api/finance/expense-entries/"
OCT, OCT_END = date(2026, 10, 1), date(2026, 10, 31)
NOV, NOV_END = date(2026, 11, 1), date(2026, 11, 30)


class AssetInstallmentTests(PnlCase):
    def setUp(self):
        super().setUp()
        self.kind = ExpenseKind.objects.get(code=ExpenseKind.EQUIPMENT)

    def card(self, amount="120000", day="2026-10-25", **extra):
        body = {"kind": self.kind.id, "name": "Станок ЧПУ", "amount": amount, "spent_at": day,
                "is_cashless": True, **extra}
        return self.client.post(URL, body, format="json")

    def pay(self, card_id, amount, day, account="BANK"):
        return self.client.post(URL, {
            "kind": self.kind.id, "asset": card_id, "amount": amount, "spent_at": day,
            "account": account, "name": "Платёж по станку",
        }, format="json")

    def test_full_scenario(self):
        r = self.card()
        self.assertEqual(r.status_code, 201, r.data)
        card_id = r.data["id"]
        self.assertTrue(r.data["is_capitalized"])
        self.assertEqual(r.data["useful_life_months"], 60)
        self.assertFalse(CashEntry.objects.exists())                       # карточка денег не двигает
        self.assertEqual(self.pay(card_id, "40000", "2026-10-25").status_code, 201)
        self.assertEqual(self.pay(card_id, "80000", "2026-11-25").status_code, 201)

        self.assertEqual(CashEntry.balance("BANK"), D("-120000"))
        # Амортизация — от полной цены: 120 000 / 60 = 2 000 с ноября.
        self.assertEqual(pnl(OCT, OCT_END)["depreciation"], D("0"))
        self.assertEqual(pnl(NOV, NOV_END)["depreciation"], D("2000.00"))
        for first, last in ((OCT, OCT_END), (NOV, NOV_END)):
            p = pnl(first, last)
            self.assertEqual(p["opex"]["total"], D("0"))                  # платежей в расходах нет
        # ОДДС: платёж — инвестиционная деятельность своего месяца.
        self.assertEqual(cash_flow(OCT, OCT_END)["sections"]["investing"]["total"], D("-40000"))
        self.assertEqual(cash_flow(NOV, NOV_END)["sections"]["investing"]["total"], D("-80000"))
        for first, last in ((OCT, OCT_END), (NOV, NOV_END), (OCT, NOV_END)):
            self.assertEqual(bridge_mod.bridge(first, last)["unexplained"], D("0"))

    def test_summary_paid_column_counts_payments_not_the_card(self):
        card_id = self.card().data["id"]
        self.pay(card_id, "40000", "2026-10-25")
        inv = finance_summary(OCT, OCT_END)["investments"]
        row = next(r for r in inv["rows"] if r["code"] == "EQUIPMENT")
        self.assertEqual(row["amount"], D("40000"))
        self.assertEqual(inv["capitalized"], D("120000"))                # актив на полную цену

    def test_payments_cannot_exceed_the_price(self):
        card_id = self.card().data["id"]
        self.assertEqual(self.pay(card_id, "100000", "2026-10-25").status_code, 201)
        r = self.pay(card_id, "20000.01", "2026-11-25")
        self.assertEqual(r.status_code, 400)
        self.assertIn("amount", r.data)
        self.assertEqual(self.pay(card_id, "20000", "2026-11-25").status_code, 201)

    def test_payment_must_target_a_card_of_the_same_kind(self):
        plain = self.client.post(URL, {"kind": self.kind.id, "amount": "30000", "spent_at": "2026-10-01"},
                                 format="json").data["id"]
        r = self.pay(plain, "100", "2026-10-25")
        self.assertEqual(r.status_code, 400)                             # обычная покупка — не карточка
        card_id = self.card().data["id"]
        other = ExpenseKind.objects.get(code=ExpenseKind.IMPROVEMENT)
        r = self.client.post(URL, {"kind": other.id, "asset": card_id, "amount": "100",
                                   "spent_at": "2026-10-25"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_only_investment_kinds_can_have_a_card(self):
        rent = ExpenseKind.objects.get(code="RENT")
        r = self.client.post(URL, {"kind": rent.id, "amount": "100000", "spent_at": "2026-10-01",
                                   "is_cashless": True}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_cheap_asset_is_still_a_card_when_the_owner_says_so(self):
        r = self.card(amount="9000")                                     # ниже порога 20 000
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["is_capitalized"])

    def test_card_with_payments_is_not_deleted(self):
        card_id = self.card().data["id"]
        pay_id = self.pay(card_id, "40000", "2026-10-25").data["id"]
        self.assertEqual(self.client.delete(f"{URL}{card_id}/").status_code, 400)
        self.assertEqual(self.client.delete(f"{URL}{pay_id}/").status_code, 204)
        self.assertEqual(self.client.delete(f"{URL}{card_id}/").status_code, 204)
        self.assertFalse(ExpenseEntry.objects.exists())

    def test_cashless_flag_cannot_be_flipped_later(self):
        plain = self.client.post(URL, {"kind": self.kind.id, "amount": "30000", "spent_at": "2026-10-01"},
                                 format="json").data["id"]
        r = self.client.patch(f"{URL}{plain}/", {"is_cashless": True}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_card_edit_keeps_cash_untouched(self):
        card_id = self.card().data["id"]
        self.pay(card_id, "40000", "2026-10-25")
        r = self.client.patch(f"{URL}{card_id}/", {"amount": "130000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance("BANK"), D("-40000"))         # касса не задета
        self.assertEqual(pnl(NOV, NOV_END)["depreciation"], D("2166.66"))
