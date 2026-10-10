"""Сверка ОПиУ → ОДДС для оплат без денег (волна 2): списание долга
(`Payment.WRITE_OFF`) и зачёт сдачи (`Payment.CHANGE`).

Цель D-11 — «Не объяснено» = 0 в любом месяце. Уровень «Долг клиентов» в
сверке считается по чекам и кассе и списанием не уменьшается (D-103): разницу
с долгом карточек закрывает строка «Списанные долги клиентов».
"""
from datetime import date
from decimal import Decimal

from clients.models import Client
from finance.reports import bridge as bridge_mod
from finance.tests_reports_calc import PnlCase, noon
from sales import sale_service
from sales.models import Payment, Receipt

D = Decimal
SEP = (date(2026, 9, 1), date(2026, 9, 30))
OCT = (date(2026, 10, 1), date(2026, 10, 31))


class BridgeNoCashPaymentsTests(PnlCase):
    def setUp(self):
        super().setUp()
        self.buyer = Client.objects.create(full_name="Тахир", phone="+996700000001")

    def order(self, day, qty, paid=None):
        return sale_service.create_sale(
            client=self.buyer, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.plate, "quantity": D(qty), "mode": "PIECE"}],
            amount_paid=D(paid) if paid is not None else None, created_at=noon(day),
        )

    def line(self, b, key):
        return next(x["amount"] for x in b["lines"] if x["key"] == key)

    def client_debt(self):
        return sum((r.debt for r in Receipt.objects.filter(client=self.buyer)), D("0"))

    def assert_zero(self, *periods):
        for first, last in periods:
            with self.subTest(period=(first, last)):
                self.assertEqual(bridge_mod.bridge(first, last)["unexplained"], D("0"))

    # --- списание долга ---------------------------------------------------------
    def test_write_off_same_month(self):
        r = self.order(date(2026, 10, 5), 40)                                # 12 000 в долг
        sale_service.apply_payment(r, None, user=self.admin, method="WRITE_OFF",
                                   paid_on=date(2026, 10, 20), note="уехал")
        self.assert_zero(OCT)
        b = bridge_mod.bridge(*OCT)
        self.assertEqual(self.line(b, "written_off"), D("12000"))
        self.assertEqual(self.line(b, "receivables"), D("-12000"))
        # Долг карточки = уровень сверки − списанное.
        self.assertEqual(self.client_debt(), D("0"))
        self.assertEqual(b["levels"]["receivables"] - self.line(b, "written_off"), self.client_debt())

    def test_write_off_next_month_and_partial_cash(self):
        r = self.order(date(2026, 9, 10), 40)                                # 12 000 в долг
        sale_service.apply_payment(r, D("2000"), user=self.admin, paid_on=date(2026, 10, 1))
        sale_service.apply_payment(r, None, user=self.admin, method="WRITE_OFF",
                                   paid_on=date(2026, 10, 15), note="банкрот")
        self.assert_zero(SEP, OCT, (SEP[0], OCT[1]))
        b = bridge_mod.bridge(*OCT)
        self.assertEqual(self.line(b, "written_off"), D("10000"))
        self.assertEqual(self.line(b, "receivables"), D("2000"))             # погасили деньгами
        self.assertEqual(self.client_debt(), D("0"))
        self.assertEqual(b["levels"]["receivables"] - D("10000"), self.client_debt())

    def test_cancelled_write_off_leaves_no_trace(self):
        r = self.order(date(2026, 10, 5), 40)
        sale_service.apply_payment(r, None, user=self.admin, method="WRITE_OFF",
                                   paid_on=date(2026, 10, 20), note="ошибка")
        pay = Payment.objects.get(receipt=r, method=Payment.Method.WRITE_OFF)
        sale_service.cancel_payment(r, pay.pk, user=self.admin, reason="ошиблись")
        self.assert_zero(OCT)
        b = bridge_mod.bridge(*OCT)
        self.assertEqual(self.line(b, "written_off"), D("0"))
        self.assertEqual(b["levels"]["receivables"], self.client_debt())
        self.assertEqual(self.client_debt(), D("12000"))

    # --- зачёт сдачи ---------------------------------------------------------------
    def test_change_offset_into_debt_of_another_month(self):
        self.order(date(2026, 9, 10), 3, paid="1500")                        # 900, сдача 600
        b_order = self.order(date(2026, 10, 5), 10)                          # 3 000 в долг
        sale_service.apply_payment(b_order, D("0"), user=self.admin, use_change=True,
                                   paid_on=date(2026, 10, 20))
        self.assertTrue(Payment.objects.filter(receipt=b_order, method=Payment.Method.CHANGE).exists())
        self.assert_zero(SEP, OCT, (SEP[0], OCT[1]))
        b = bridge_mod.bridge(*OCT)
        self.assertEqual(self.client_debt(), D("2400"))
        # Сдача внутри клиента гасит его долг: уровни сверки = карточке.
        self.assertEqual(b["levels"]["receivables"], self.client_debt())
        self.assertEqual(b["levels"]["client_money"], D("0"))
        self.assertEqual(self.line(b, "written_off"), D("0"))

    def test_cancelled_change_offset_stays_zero(self):
        self.order(date(2026, 9, 10), 3, paid="1500")
        b_order = self.order(date(2026, 10, 5), 10)
        sale_service.apply_payment(b_order, D("0"), user=self.admin, use_change=True,
                                   paid_on=date(2026, 10, 20))
        pay = Payment.objects.get(receipt=b_order, method=Payment.Method.CHANGE)
        sale_service.cancel_payment(b_order, pay.pk, user=self.admin, reason="не та сдача")
        self.assert_zero(SEP, OCT)
        b = bridge_mod.bridge(*OCT)
        self.assertEqual(self.client_debt(), D("3000"))
        self.assertEqual(b["levels"]["receivables"] - b["levels"]["client_money"],
                         self.client_debt() - D("600"))
