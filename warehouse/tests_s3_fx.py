"""S3 перепроверки владельца 10.10 — накладная в валюте (RU-N16, RU-N17, RU-N18).

Сценарий экранного прогона (s4.log): накладная #4 на 415 USD по 87,45 (355 + 60),
при приёмке оплачено 200 сом наличными; потом 150 USD по 88,1. Окно оплаты
писало «С кассы уйдёт 13 215, долга закроется на 13 118, курсовая разница 98»,
а касса — 13 118 + 98 = 13 216; «Курсовая разница: Курсовая разница: … вместо
87.450000».
"""
from decimal import Decimal

from finance.models import CashEntry, ExpenseEntry
from finance.reports.bridge import bridge
from warehouse.models import SupplierPayment, Supply
from warehouse.tests_recheck_stock import OCT, SUPPLIES, Base

D = Decimal
PAYMENTS = "/api/warehouse/supplier-payments/"


class ForeignSupplyBase(Base):
    def setUp(self):
        super().setUp()
        r = self.client.post(SUPPLIES, {
            "number": "#4", "supplier": self.sup.id, "received_on": "2026-10-10", "currency": "USD",
            "rate": "87.45", "paid_amount": "200", "paid_account": "CASH",
            "lines": [
                {"material": self.m.id, "form": "SHEET", "width": "1.22", "height": "2.44", "sheet_count": "10",
                 "cost_fc": "355"},
                {"material": self.m.id, "form": "SHEET", "width": "1.22", "height": "2.44", "sheet_count": "5",
                 "cost_fc": "60"},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.doc = Supply.objects.get(pk=r.data["id"])

    def pay(self, amount, rate, expect=200, **extra):
        r = self.client.post(f"{SUPPLIES}{self.doc.id}/pay/", {
            "amount": amount, "rate": rate, "account": "CASH", "paid_on": "2026-10-10", **extra,
        }, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def cash_out(self, payment):
        """Сколько по этому платежу ушло из кассы: платёж + курсовая разница."""
        rows = CashEntry.objects.filter(pk__in=[payment.cash_entry_id] + (
            list(CashEntry.objects.filter(expense=payment.fx_expense).values_list("pk", flat=True))
            if payment.fx_expense_id else []))
        return {e.pk: e for e in rows}


class CashLinesMatchTheWindowTests(ForeignSupplyBase):
    """RU-N17: строки кассы сходятся с окном до сома; описание без дубля и хвоста нулей."""

    def test_partial_payment_splits_into_whole_fx(self):
        self.pay("150", "88.1")
        p = SupplierPayment.objects.get(kind=SupplierPayment.Kind.PAYMENT)
        self.assertEqual(p.amount, D("13215.00"), "150 × 88,1 — ровно то, что в окне")
        self.assertEqual(p.fx_diff, D("98"), "курсовая разница — целыми сомами")
        self.assertEqual(p.settled, D("13117.00"))
        entries = self.cash_out(p)
        self.assertEqual(sorted(e.amount for e in entries.values()), [D("98"), D("13117.00")])
        self.assertEqual(sum(e.amount for e in entries.values()), D("13215.00"))
        self.assertEqual(bridge(*OCT)["unexplained"], D("0"))

    def test_closing_payment_closes_the_debt_and_adds_up(self):
        self.pay("150", "88.1")
        doc = Supply.objects.get(pk=self.doc.pk)
        left_fc, left = doc.debt_foreign, doc.debt
        self.pay(str(left_fc), "88")
        doc = Supply.objects.get(pk=self.doc.pk)
        self.assertEqual((doc.debt, doc.debt_foreign), (D("0"), D("0")))
        last = SupplierPayment.objects.filter(kind=SupplierPayment.Kind.PAYMENT).order_by("-id").first()
        self.assertEqual(last.settled, left)
        self.assertEqual(last.fx_diff, last.fx_diff.to_integral_value())
        self.assertLess(abs(last.amount - (left_fc * D("88")).quantize(D("0.01"))), D("0.5"))
        self.assertEqual(sum(e.amount for e in self.cash_out(last).values()), last.amount)
        self.assertEqual(bridge(*OCT)["unexplained"], D("0"))

    def test_description_has_no_double_label_and_no_trailing_zeros(self):
        self.pay("150", "88.1")
        fx = ExpenseEntry.objects.get(kind__code="FX_DIFF")
        self.assertEqual(fx.name, "Оплата накладной #4: 150 USD по 88,1 вместо 87,45")
        note = CashEntry.objects.get(expense=fx).note
        self.assertEqual(note.count("Курсовая разница"), 1, note)
        self.assertNotIn("87.450000", note)

    def test_income_is_whole_too(self):
        self.pay("150", "86.3")
        p = SupplierPayment.objects.get(kind=SupplierPayment.Kind.PAYMENT)
        # 150 × 86,3 = 12 945; закрыто по 87,45 = 13 117,50 → доход 172,50 → 173.
        self.assertEqual(p.amount, D("12945.00"))
        self.assertEqual(p.fx_diff, D("-173"))
        self.assertEqual(p.settled, D("13118.00"))
        self.assertEqual(sum(e.amount for e in self.cash_out(p).values()), D("12945.00"))


class PaidDebtVsPaidMoneyTests(ForeignSupplyBase):
    """RU-N16: «закрыто долга» и «уплачено деньгами» раздельно."""

    def test_two_figures(self):
        data = self.pay("150", "88.1")
        self.assertEqual(D(str(data["paid_total"])), D("13317.00"), "закрыто долга: 200 + 13 117")
        self.assertEqual(D(str(data["paid_cash"])), D("13415.00"), "уплачено деньгами: 200 + 13 215")
        self.client.force_authenticate(self.keeper)
        data = self.client.get(f"{SUPPLIES}{self.doc.id}/").data
        self.assertIsNone(data["paid_cash"])

    def test_som_supply_has_equal_figures(self):
        r = self.supply("S-1", "2026-10-09", [self.sheet_line(2, 7000)], paid_amount="3000", paid_account="CASH")
        self.assertEqual(D(str(r["paid_cash"])), D("3000"))
        self.assertEqual(D(str(r["paid_total"])), D("3000"))


class RateTypoNeedsConfirmationTests(ForeignSupplyBase):
    """RU-N18: курс оплаты дальше 10 % от курса накладной — 409 с подтверждением."""

    def test_typo_in_the_rate_asks_first(self):
        data = self.pay("150", "881", expect=409)
        self.assertTrue(data["needs_confirmation"])
        self.assertEqual(data["code"], "rate_far")
        self.assertIn("87,45", data["detail"])
        self.assertFalse(SupplierPayment.objects.filter(kind=SupplierPayment.Kind.PAYMENT).exists())
        self.assertEqual(CashEntry.balance("CASH"), D("-200"))
        self.pay("150", "881", confirm_rate=True)
        self.assertTrue(SupplierPayment.objects.filter(kind=SupplierPayment.Kind.PAYMENT).exists())

    def test_ten_percent_passes_quietly(self):
        self.pay("10", "96.19")   # +9,99 %
        self.pay("10", "78.71")   # −9,99 %

    def test_payments_endpoint_asks_too(self):
        r = self.client.post(PAYMENTS, {"supply": self.doc.id, "amount": "10", "rate": "8.745",
                                        "account": "CASH", "paid_on": "2026-10-10"}, format="json")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertTrue(r.data["needs_confirmation"])
