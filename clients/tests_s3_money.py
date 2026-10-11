"""Перепроверка владельца 10.10, S3 клиентов (сценарии C1, C3, C6, B7c).

- RM-N8: входящий остаток проводился датой закрытого месяца → 400.
- RM-N9: в приёмах денег клиента не было потолка суммы и проверки тыйынов:
  телефон «996700100200» в колонке суммы давал долг 996 млрд, аванс «0,001»
  проходил → потолок 10^10 и не больше двух знаков — 400; телефон в сумме
  входящих остатков — ошибка строки предпросмотра.
"""
from datetime import date

from clients.models import ClientAdvance, OpeningBalance
from clients.opening import parse_number
from clients.testkit import D, ShopCase
from finance.models import CashEntry, PeriodLock

OPENING = "/api/clients/opening-balances/"


class OpeningClosedMonthTests(ShopCase):
    def test_opening_balance_in_closed_month_is_refused(self):
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 9, 30)
        lock.save()
        r = self.client.post(OPENING, {"text": "+996555999888;Новый;3000", "as_of": "2026-09-15"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("период закрыт", r.data["detail"])
        self.assertFalse(OpeningBalance.objects.exists())
        r = self.client.post(OPENING, {"text": "+996555999888;Новый;3000", "as_of": "2026-10-01"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)


class OpeningAmountTests(ShopCase):
    def preview_row(self, amount):
        r = self.client.post(OPENING + "preview/", {"text": f"+996700100201;Бакыт;{amount}"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["rows"][0]

    def test_phone_in_amount_is_a_row_error(self):
        for amount in ("996700100200", "0555112233", "0555 11 22 33"):
            with self.subTest(amount=amount):
                row = self.preview_row(amount)
                self.assertEqual(row["status"], "error")
                self.assertIn("телефон", " ".join(row["errors"]))
        r = self.client.post(OPENING, {"text": "+996700100201;Бакыт;996700100200", "as_of": "2026-10-01"},
                             format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(OpeningBalance.objects.exists())

    def test_ceiling_and_cents(self):
        with self.assertRaises(ValueError):
            parse_number("10 000 000 000")
        with self.assertRaises(ValueError):
            parse_number("12 000,505")
        self.assertEqual(parse_number("9 999 999 999,99"), D("9999999999.99"))
        self.assertEqual(parse_number("12 000,50"), D("12000.50"))
        self.assertEqual(self.preview_row("15 000 000 000")["status"], "error")
        self.assertEqual(self.preview_row("12 000,50")["status"], "create")


class AdvanceAmountTests(ShopCase):
    def advance(self, amount):
        return self.client.post(f"/api/clients/clients/{self.agency.id}/advances/",
                                {"amount": amount, "method": "CASH"}, format="json")

    def test_cents_and_ceiling(self):
        for amount in ("0.001", "100.555", "10000000000", "1e15"):
            with self.subTest(amount=amount):
                r = self.advance(amount)
                self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(ClientAdvance.objects.exists())
        self.assertFalse(CashEntry.objects.exists())
        r = self.advance("5000.50")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ClientAdvance.objects.get().amount, D("5000.50"))


class PayDebtAmountTests(ShopCase):
    def test_pay_debt_and_write_off_refuse_bad_amounts(self):
        self.sale(5000)
        url = f"/api/clients/clients/{self.agency.id}/pay-debt/"
        for amount in ("0.001", "10000000000", "996700100200"):
            for method in ("CASH", "WRITE_OFF"):
                with self.subTest(amount=amount, method=method):
                    r = self.client.post(url, {"amount": amount, "method": method, "note": "x"}, format="json")
                    self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(D(str(self.card()["debt"])), D("5000"))
        r = self.client.post(url, {"amount": "1000.50", "method": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(self.card()["debt"])), D("3999.50"))
