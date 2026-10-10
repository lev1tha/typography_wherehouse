"""Перепроверка владельца 10.10 (docs/OWNER_RECHECK_2026-10-10.md), зона «Клиенты».

- CLI-14 (S1): «Общая выплата», «Принять аванс» и проведение входящих остатков
  не защищались от двойной отправки — заголовок `Idempotency-Key` форма слала,
  сервер его не читал. Сценарий C5: один ключ дважды — одна кассовая запись,
  один аванс.
- RM-N5 (S2): «Общая выплата» принимала лишний ноль (37 000 при долге 3 700) без
  вопроса, хотя `/pay/` спрашивает. Сценарии F2/F3.
- RM-N6 (S2): аванс при живом долге не гасил долг. Сценарий A2: входящий долг
  12 000, аванс 5 000 с зачётом — долг 7 000.
"""
from django.utils import timezone

from clients.models import ClientAdvance, OpeningBalance, OpeningDebtPayment
from clients.testkit import D, ShopCase
from finance.models import CashEntry
from finance.reports.bridge import bridge
from sales.models import Payment


def key(value):
    return {"HTTP_IDEMPOTENCY_KEY": value}


class DoubleSubmitTests(ShopCase):
    """CLI-14: повтор с тем же ключом ничего не проводит второй раз."""

    def pay_debt(self, body, **extra):
        return self.client.post(
            f"/api/clients/clients/{self.agency.id}/pay-debt/", body, format="json", **extra,
        )

    def advance(self, body, **extra):
        return self.client.post(
            f"/api/clients/clients/{self.agency.id}/advances/", body, format="json", **extra,
        )

    def test_pay_debt_same_key_twice_is_one_payment(self):
        self.sale(2000, paid=0)
        before = CashEntry.objects.count()
        r1 = self.pay_debt({"amount": "1000", "method": "CASH"}, **key("k-123"))
        r2 = self.pay_debt({"amount": "1000", "method": "CASH"}, **key("k-123"))
        self.assertEqual(r1.status_code, 200, r1.data)
        self.assertEqual(r2.status_code, 200, r2.data)
        self.assertTrue(r2.data.get("idempotent_replay"))
        self.assertEqual(CashEntry.objects.count() - before, 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(D(str(self.card()["debt"])), D("1000"))
        self.assertEqual(D(str(r2.data["debt"])), D("1000"))

    def test_pay_debt_new_key_is_a_new_payment(self):
        self.sale(2000, paid=0)
        self.pay_debt({"amount": "500", "method": "CASH"}, **key("a"))
        self.pay_debt({"amount": "500", "method": "CASH"}, **key("b"))
        self.assertEqual(D(str(self.card()["debt"])), D("1000"))

    def test_pay_debt_without_key_works_as_before(self):
        self.sale(2000, paid=0)
        r = self.pay_debt({"amount": "500", "method": "CASH"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertNotIn("idempotent_replay", r.data)

    def test_bad_key_is_400(self):
        self.sale(2000, paid=0)
        r = self.pay_debt({"amount": "500", "method": "CASH"}, **key("пробел и мусор!"))
        self.assertEqual(r.status_code, 400, r.data)
        r = self.advance({"amount": "500", "method": "CASH"}, **key("x" * 101))
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(CashEntry.objects.exists())

    def test_advance_same_key_twice_is_one_advance(self):
        before = CashEntry.objects.count()
        r1 = self.advance({"amount": "700", "method": "CASH"}, **key("a-1"))
        r2 = self.advance({"amount": "700", "method": "CASH"}, **key("a-1"))
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertIn(r2.status_code, (200, 201), r2.data)
        self.assertTrue(r2.data.get("idempotent_replay"))
        self.assertEqual(CashEntry.objects.count() - before, 1)
        self.assertEqual(ClientAdvance.objects.filter(client=self.agency, amount=700).count(), 1)
        self.assertEqual(r2.data["id"], r1.data["id"])

    def test_key_of_another_user_is_not_a_replay(self):
        self.advance({"amount": "700", "method": "CASH"}, **key("same"))
        self.client.force_authenticate(self.store)
        from clients.models import ClientSettings

        ClientSettings.objects.update_or_create(pk=1, defaults={"storekeeper_takes_debt": True})
        r = self.advance({"amount": "700", "method": "CASH"}, **key("same"))
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ClientAdvance.objects.count(), 2)

    def test_opening_balances_same_key_twice_is_one_batch(self):
        body = {"text": "+996700100200;Бакыт;12000", "as_of": "2026-09-30"}
        r1 = self.client.post("/api/clients/opening-balances/", body, format="json", **key("o-1"))
        r2 = self.client.post("/api/clients/opening-balances/", body, format="json", **key("o-1"))
        self.assertEqual(r1.status_code, 201, r1.data)
        self.assertEqual(r2.status_code, 201, r2.data)
        self.assertTrue(r2.data.get("idempotent_replay"))
        self.assertEqual(OpeningBalance.objects.count(), 1)
        self.assertEqual(r2.data["batch"], r1.data["batch"])
        self.assertEqual(len(r2.data["rows"]), 1)


class PayDebtOverpayTests(ShopCase):
    """RM-N5: лишний ноль в «Общей выплате» — сначала вопрос, как у `/pay/`."""

    def pay_debt(self, body):
        return self.client.post(f"/api/clients/clients/{self.agency.id}/pay-debt/", body, format="json")

    def test_ten_times_the_debt_asks_first(self):
        self.sale(3700, paid=0)
        r = self.pay_debt({"amount": "37000", "method": "CASH"})
        self.assertEqual(r.status_code, 409, r.data)
        self.assertTrue(r.data["needs_confirmation"])
        warning = r.data["warnings"][0]
        self.assertEqual(warning["code"], "overpay")
        self.assertEqual(D(str(warning["amount"])), D("37000"))
        self.assertEqual(D(str(warning["debt"])), D("3700"))
        self.assertFalse(CashEntry.objects.exists())
        self.assertEqual(D(str(self.card()["debt"])), D("3700"))

        r = self.pay_debt({"amount": "37000", "method": "CASH", "confirm_overpay": True})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["change"])), D("33300"))
        self.assertEqual(CashEntry.balance("CASH"), D("37000"))

    def test_three_times_is_asked_less_is_not(self):
        self.sale(1000, paid=0)
        self.assertEqual(self.pay_debt({"amount": "3000", "method": "CASH"}).status_code, 409)
        r = self.pay_debt({"amount": "2999", "method": "CASH"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["change"])), D("1999"))

    def test_debt_of_the_chosen_orders_and_opening_debt_counts(self):
        """Порог — от выбранного долга: входящий долг тоже долг."""
        a = self.sale(1000, paid=0)
        self.client.post("/api/clients/opening-balances/",
                         {"text": f"{self.agency.phone};Ак Жол;5000", "as_of": "2026-09-30"}, format="json")
        # весь долг 6 000 — 15 000 меньше трёх раз
        self.assertEqual(self.pay_debt({"amount": "15000", "method": "CASH"}).status_code, 200)
        b = self.sale(1000, paid=0)
        # выбран один заказ на 1 000 — 5 000 уже переплата в 5 раз
        r = self.pay_debt({"amount": "5000", "method": "CASH", "receipt_ids": [str(b.id)]})
        self.assertEqual(r.status_code, 409, r.data)
        a.refresh_from_db()
        self.assertEqual(a.debt, D("0"))

    def test_write_off_is_never_asked(self):
        self.sale(1000, paid=0)
        r = self.pay_debt({"amount": "1000", "method": "WRITE_OFF", "note": "уехал"})
        self.assertEqual(r.status_code, 200, r.data)


class AdvanceOffsetsDebtTests(ShopCase):
    """RM-N6: «Принять аванс» при живом долге сначала гасит долг (по умолчанию)."""

    def advance(self, body):
        return self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", body, format="json")

    def opening(self, amount):
        r = self.client.post(
            "/api/clients/opening-balances/",
            {"text": f"{self.agency.phone};Ак Жол;{amount}", "as_of": "2026-09-30"}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)

    def test_a2_opening_debt_12000_advance_5000_with_offset(self):
        self.opening("12000")
        r = self.advance({"amount": "5000", "method": "CASH", "offset_debt": True})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["to_debt"])), D("5000"))
        self.assertIsNone(r.data["advance"])
        self.assertEqual(D(str(r.data["debt"])), D("7000"))
        card = self.card()
        self.assertEqual(D(str(card["debt"])), D("7000"))
        self.assertEqual(D(str(card["advance_balance"])), D("0"))
        self.assertEqual(D(str(card["balance"])), D("7000"))
        self.assertEqual(CashEntry.balance("CASH"), D("5000"))
        self.assertEqual(OpeningDebtPayment.objects.get().amount, D("5000"))
        self.assertFalse(ClientAdvance.objects.filter(is_opening=False).exists())
        self.assertEqual(D(str(self.statement()["closing"])), D("7000"))
        today = timezone.localdate()
        self.assertEqual(bridge(today.replace(day=1), today)["unexplained"], D("0"))

    def test_offset_is_the_default(self):
        self.opening("12000")
        r = self.advance({"amount": "5000", "method": "CASH"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(self.card()["debt"])), D("7000"))

    def test_without_offset_the_advance_waits(self):
        self.opening("12000")
        r = self.advance({"amount": "5000", "method": "CASH", "offset_debt": False})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["to_debt"])), D("0"))
        card = self.card()
        self.assertEqual(D(str(card["debt"])), D("12000"))
        self.assertEqual(D(str(card["advance_balance"])), D("5000"))
        self.assertEqual(D(str(card["balance"])), D("7000"))

    def test_more_than_the_debt_the_rest_is_an_advance(self):
        old = self.sale(3000, paid=0, days_ago=5)
        r = self.advance({"amount": "5000", "method": "MBANK"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["to_debt"])), D("3000"))
        self.assertEqual(D(str(r.data["advance"]["amount"])), D("2000"))
        old.refresh_from_db()
        self.assertEqual(old.debt, D("0"))
        self.assertEqual(old.change_due, D("0"))          # не сдача — аванс
        card = self.card()
        self.assertEqual(D(str(card["debt"])), D("0"))
        self.assertEqual(D(str(card["advance_balance"])), D("2000"))
        self.assertEqual(CashEntry.balance("BANK"), D("5000"))
        self.assertEqual(D(str(self.statement()["closing"])), D("-2000"))

    def test_no_debt_is_a_plain_advance(self):
        r = self.advance({"amount": "700", "method": "CASH"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["amount"])), D("700"))
        self.assertEqual(D(str(r.data["to_debt"])), D("0"))
        self.assertEqual(ClientAdvance.objects.get().amount, D("700"))
