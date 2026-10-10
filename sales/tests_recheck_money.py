"""Перепроверка владельца 10.10 (docs/OWNER_RECHECK_2026-10-10.md), зона «оплаты
и списания долга по заказам» (D-155…D-158).

- RM-N1 (S1, сценарий E2): возврат заказа со списанным долгом выдавал из кассы
  деньги, которых клиент не платил (касса 0 → −7 400), а расход «Безнадёжные
  долги» оставался. Теперь возврат сначала уменьшает списание и его расход,
  деньги отдаются только из реальных оплат.
- RM-N2 (S1, E1): удаление заказа со списанным долгом оставляло расход
  «Безнадёжные долги» — прибыль −3 700 за несуществующий заказ.
- RM-N3 (S1, B8a): отмена списания по заказу в закрытом месяце проходила и
  меняла его прибыль. Замок — по дате списания, как у входящего долга (D-141).
- RM-N4 (S2, E4): отмена оплаты заказа удаляла запись оплаты и переписывала акт
  закрытого месяца (на 30.09 было 14 400, стало 17 400). Теперь оплата остаётся
  своим днём, отмена — встречной записью днём отмены; то же у «Отката оплаты».

Везде: долг одинаков на карточке, в акте, в списке должников, в плитке «Долг» и
в корзинах возраста; сверка ОПиУ → ОДДС «Не объяснено» = 0.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from clients.testkit import D, ShopCase
from finance.models import CashEntry, ExpenseEntry, PeriodLock
from finance.periods import month_end
from finance.reports.bridge import bridge
from sales.models import Payment, Receipt
from sales.sale_service import writeoff_total


class MoneyCase(ShopCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.m0 = self.today.replace(day=1)
        self.prev_end = self.m0 - timedelta(days=1)
        self.prev = self.prev_end.replace(day=1)

    # --- даты и замок -------------------------------------------------------
    def on_prev(self, day):
        return self.prev_end.replace(day=day)

    def close_prev_month(self):
        lock = PeriodLock.load()
        lock.closed_through = self.prev_end
        lock.save()

    # --- действия через API -------------------------------------------------
    def act(self, receipt, action, body=None, expect=200):
        r = self.client.post(f"/api/sales/receipts/{receipt.id}/{action}/", body or {}, format="json")
        self.assertEqual(r.status_code, expect, getattr(r, "data", r))
        return r

    def delete(self, receipt, expect=204):
        r = self.client.delete(f"/api/sales/receipts/{receipt.id}/")
        self.assertEqual(r.status_code, expect, getattr(r, "data", r))
        return r

    def write_off(self, receipt, amount=None, on=None, expect=200):
        body = {"note": "клиент пропал"}
        if amount is not None:
            body["amount"] = str(amount)
        if on is not None:
            body["paid_on"] = on.isoformat()
        return self.act(receipt, "write-off", body, expect=expect)

    def pay_api(self, receipt, amount, on=None, method="CASH"):
        body = {"amount": str(amount), "method": method}
        if on is not None:
            body["paid_on"] = on.isoformat()
        return self.act(receipt, "pay", body)

    def cancel(self, receipt, payment, expect=200, reason="ошибка"):
        return self.act(receipt, "cancel-payment", {"payment": payment.id, "reason": reason}, expect=expect)

    def refund(self, receipt, qty=None, expect=200):
        body = {"reason": "вернул товар"}
        if qty is not None:
            item = receipt.items.filter(is_returned=False).first()
            body["quantities"] = [{"id": item.id, "quantity": str(qty)}]
        return self.act(receipt, "refund", body, expect=expect)

    # --- цифры ----------------------------------------------------------------
    def cash(self):
        return CashEntry.balance("CASH") + CashEntry.balance("BANK")

    def bad_debt(self, d_from=None, d_to=None):
        qs = ExpenseEntry.objects.filter(kind__code="BAD_DEBT")
        if d_from:
            qs = qs.filter(spent_at__gte=d_from)
        if d_to:
            qs = qs.filter(spent_at__lte=d_to)
        return sum((e.amount for e in qs), D("0"))

    def report(self, d_from, d_to):
        r = self.client.get("/api/finance/report/", {"date_from": d_from.isoformat(), "date_to": d_to.isoformat()})
        self.assertEqual(r.status_code, 200, getattr(r, "data", r))
        return r.data

    def profit(self, d_from, d_to):
        return D(str(self.report(d_from, d_to)["profit"]))

    def act_closing(self, d_from=None, d_to=None):
        params = {}
        if d_from:
            params["date_from"] = d_from.isoformat()
        if d_to:
            params["date_to"] = d_to.isoformat()
        return D(str(self.statement(**params)["closing"]))

    def payments(self, receipt):
        r = self.client.get(f"/api/sales/receipts/{receipt.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["payments"]

    def check(self, tag=""):
        """Одна цифра долга на всех экранах; акт без дат = сальдо карточки;
        «Не объяснено» 0 в прошлом и текущем месяце."""
        card = self.card()
        debt = D(str(card["debt"]))
        lst = self.client.get("/api/clients/clients/", {"has_debt": 1}).data
        rows = lst["results"] if isinstance(lst, dict) and "results" in lst else lst
        row = next((x for x in rows if x["id"] == self.agency.id), None)
        self.assertEqual(D(str(row["debt"])) if row else D("0"), debt, f"[{tag}] список должников")
        tile = self.report(self.today - timedelta(days=400), self.today)["client_debt"]
        self.assertEqual(D(str(tile)), debt, f"[{tag}] плитка «Долг»")
        aging = self.client.get("/api/clients/clients/aging/").data["total"]
        self.assertEqual(D(str(aging)), debt, f"[{tag}] корзины возраста")
        self.assertEqual(self.act_closing(), D(str(card["balance"])), f"[{tag}] акт без дат ≠ сальдо")
        for first, last in ((self.prev, self.prev_end), (self.m0, month_end(self.today))):
            self.assertEqual(bridge(first, last)["unexplained"], D("0"), f"[{tag}] «Не объяснено» {first:%m}")
        return debt


class RefundOfWrittenOffOrderTests(MoneyCase):
    """RM-N1, сценарий E2."""

    def test_e2_full_refund_of_written_off_order_pays_nothing(self):
        r = self.sale(7400, paid=0)
        self.write_off(r)
        self.assertEqual(self.bad_debt(), D("7400"))
        cash0 = self.cash()
        self.refund(r)
        r.refresh_from_db()
        # Клиент не заплатил ни сома — и не получает ни сома.
        self.assertEqual(self.cash(), cash0)
        self.assertEqual(r.debt, D("0"))
        self.assertEqual(r.change_due, D("0"))
        # Списание и его расход ушли вместе с возвратом: прибыль месяца 0.
        self.assertEqual(writeoff_total(r), D("0"))
        self.assertEqual(self.bad_debt(), D("0"))
        self.assertEqual(self.profit(self.m0, self.today), D("0"))
        self.check("E2")

    def test_e2b_partial_refund_takes_the_write_off_first(self):
        r = self.sale(11100, paid=3700)
        self.write_off(r)                       # списано 7 400
        cash0 = self.cash()
        self.refund(r, qty=7400)                # вернул «2 листа» на 7 400
        r.refresh_from_db()
        self.assertEqual(self.cash(), cash0)    # сверх оплаченного 3 700 — ни сома
        self.assertEqual(r.amount_paid, D("3700"))
        self.assertEqual(r.debt, D("0"))
        self.assertEqual(r.change_due, D("0"))
        self.assertEqual(writeoff_total(r), D("0"))
        self.assertEqual(self.bad_debt(), D("0"))
        # В акте списание остаётся своим днём, уменьшение — встречной строкой.
        kinds = [(x["kind"], D(x["debit"]), D(x["credit"])) for x in self.statement()["rows"]]
        self.assertIn(("write_off", D("0"), D("7400")), kinds)
        self.assertIn(("write_off_cancelled", D("7400"), D("0")), kinds)
        self.check("E2b")

    def test_refund_smaller_than_the_write_off_only_reduces_it(self):
        r = self.sale(10000, paid=2000)
        self.write_off(r, amount=5000)          # долг 3 000
        cash0 = self.cash()
        self.refund(r, qty=6000)                # 3 000 гасят долг, 3 000 — списание
        r.refresh_from_db()
        self.assertEqual(self.cash(), cash0)
        self.assertEqual(writeoff_total(r), D("2000"))
        self.assertEqual(self.bad_debt(), D("2000"))
        self.assertEqual(r.debt, D("0"))
        self.check("partial")
        # Ещё возврат на 3 000: оставшееся списание 2 000, и 1 000 — реальные деньги.
        self.refund(r, qty=3000)
        r.refresh_from_db()
        self.assertEqual(writeoff_total(r), D("0"))
        self.assertEqual(self.bad_debt(), D("0"))
        self.assertEqual(self.cash(), cash0 - D("1000"))
        self.check("partial-2")

    def test_write_off_of_a_closed_month_is_reduced_on_the_refund_day(self):
        r = self.sale(7400, paid=0, on=self.on_prev(10))
        self.write_off(r, on=self.on_prev(25))
        profit_prev = self.profit(self.prev, self.prev_end)
        act_prev = self.act_closing(self.prev, self.prev_end)
        self.close_prev_month()
        cash0 = self.cash()
        self.refund(r)
        self.assertEqual(self.cash(), cash0)
        # Закрытый месяц не тронут ни в прибыли, ни в акте.
        self.assertEqual(self.profit(self.prev, self.prev_end), profit_prev)
        self.assertEqual(self.act_closing(self.prev, self.prev_end), act_prev)
        self.assertEqual(self.bad_debt(self.prev, self.prev_end), D("7400"))
        # Уменьшение расхода — днём возврата.
        self.assertEqual(self.bad_debt(self.m0, self.today), D("-7400"))
        self.check("closed write-off")


class DeleteWrittenOffOrderTests(MoneyCase):
    """RM-N2, сценарий E1."""

    def test_e1_delete_takes_the_bad_debt_expense_along(self):
        r = self.sale(3700, paid=0)
        self.write_off(r)
        self.assertEqual(self.bad_debt(), D("3700"))
        self.delete(r)
        self.assertFalse(ExpenseEntry.objects.filter(kind__code="BAD_DEBT").exists())
        self.assertEqual(self.profit(self.m0, self.today), D("0"))
        self.check("E1")

    def test_delete_after_a_refund_reduction_drops_both_expenses(self):
        r = self.sale(7400, paid=0, on=self.on_prev(10))
        self.write_off(r, on=self.on_prev(25))
        self.refund(r, qty=3700)                 # уменьшение днём возврата (месяц списания открыт)
        self.delete(r)
        self.assertFalse(ExpenseEntry.objects.filter(kind__code="BAD_DEBT").exists())
        self.check("delete after refund")

    def test_delete_is_refused_when_the_write_off_month_is_closed(self):
        r = self.sale(3700, paid=0)
        # Списание задним числом — раньше даты заказа (так можно), в прошлом месяце.
        self.write_off(r, on=self.on_prev(25))
        self.close_prev_month()
        out = self.delete(r, expect=400)
        self.assertIn("закрыт", str(out.data))
        self.assertTrue(Receipt.objects.filter(pk=r.pk).exists())
        self.assertEqual(self.bad_debt(), D("3700"))
        self.check("delete refused")


class CancelWriteOffLockTests(MoneyCase):
    """RM-N3, сценарий B8a."""

    def test_b8a_cancel_write_off_of_a_closed_month_is_refused(self):
        r = self.sale(7400, paid=0, on=self.on_prev(10))
        self.write_off(r, amount=1000, on=self.on_prev(25))
        profit_prev = self.profit(self.prev, self.prev_end)
        self.close_prev_month()
        wo = Payment.objects.get(receipt=r, method="WRITE_OFF")
        out = self.cancel(r, wo, expect=400)
        self.assertIn("закрыт", str(out.data))
        r.refresh_from_db()
        self.assertEqual(r.debt, D("6400"))
        self.assertEqual(self.bad_debt(), D("1000"))
        self.assertEqual(self.profit(self.prev, self.prev_end), profit_prev)
        self.check("B8a")

    def test_cancel_write_off_of_an_open_month_keeps_the_history(self):
        r = self.sale(7400, paid=0, on=self.on_prev(10))
        self.write_off(r, amount=1000, on=self.on_prev(25))
        act_prev = self.act_closing(self.prev, self.prev_end)
        wo = Payment.objects.get(receipt=r, method="WRITE_OFF")
        self.cancel(r, wo)
        r.refresh_from_db()
        self.assertEqual(r.debt, D("7400"))
        self.assertEqual(self.bad_debt(), D("0"))
        # Месяц открыт — расход ушёл; в акте списание осталось своим днём.
        self.assertEqual(self.act_closing(self.prev, self.prev_end), act_prev)
        self.cancel(r, wo, expect=400)          # второй раз — нечего отменять
        self.check("cancel write-off")

    def test_unpay_with_a_write_off_of_a_closed_month_is_refused(self):
        r = self.sale(7400, paid=2000, on=self.on_prev(10))
        self.write_off(r, amount=1000, on=self.on_prev(25))
        self.close_prev_month()
        cash0 = self.cash()
        out = self.act(r, "unpay", expect=400)
        self.assertIn("закрыт", str(out.data))
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, D("3000"))
        self.assertEqual(self.cash(), cash0)
        self.assertEqual(self.bad_debt(), D("1000"))
        self.check("unpay refused")


class CancelPaymentKeepsHistoryTests(MoneyCase):
    """RM-N4, сценарий E4."""

    def opening(self, amount, as_of):
        text = f"{self.agency.phone};{self.agency.display_name};{amount}"
        r = self.client.post("/api/clients/opening-balances/", {"text": text, "as_of": as_of.isoformat()}, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_e4_cancel_does_not_rewrite_the_closed_month_statement(self):
        self.opening(12000, self.prev - timedelta(days=1))
        r = self.sale(7400, paid=0, on=self.on_prev(10))
        self.pay_api(r, 3000, on=self.on_prev(21))
        act_prev = self.act_closing(self.prev, self.prev_end)
        self.assertEqual(act_prev, D("16400"))
        self.close_prev_month()
        cash0 = self.cash()
        pay = Payment.objects.get(receipt=r, method="CASH")
        self.cancel(r, pay)
        self.assertEqual(self.cash(), cash0 - D("3000"))
        # Акт закрытого месяца прежний, на сегодня — равен карточке.
        self.assertEqual(self.act_closing(self.prev, self.prev_end), act_prev)
        self.assertEqual(self.check("E4"), D("19400"))
        self.assertEqual(self.act_closing(), D("19400"))
        rows = [(x["date"], x["kind"], D(x["debit"]), D(x["credit"])) for x in self.statement()["rows"]]
        self.assertIn((self.on_prev(21).isoformat(), "payment", D("0"), D("3000")), rows)
        self.assertIn((self.today.isoformat(), "payment_cancelled", D("3000"), D("0")), rows)

    def test_cancelled_payment_is_marked_and_cannot_be_cancelled_twice(self):
        r = self.sale(1000, paid=0)
        self.pay_api(r, 300)
        self.pay_api(r, 200)
        first = Payment.objects.filter(receipt=r).order_by("id").first()
        self.cancel(r, first)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, D("200"))
        listed = {p["id"]: p for p in self.payments(r)}
        self.assertTrue(listed[first.id]["cancelled"])
        reversal = [p for p in listed.values() if p["reversal"]]
        self.assertEqual([D(str(p["amount"])) for p in reversal], [D("-300")])
        self.assertEqual(sum(1 for p in listed.values() if not p["cancelled"] and not p["reversal"]), 1)
        # Повторная отмена и отмена самой встречной записи — 400, деньги не двигаются.
        cash0 = self.cash()
        self.cancel(r, first, expect=400)
        self.cancel(r, Payment.objects.get(pk=reversal[0]["id"]), expect=400)
        self.assertEqual(self.cash(), cash0)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, D("200"))
        self.check("twice")

    def test_unpay_does_not_rewrite_the_closed_month_statement(self):
        r = self.sale(7400, paid=2000, on=self.on_prev(10))
        self.pay_api(r, 1000, on=self.on_prev(21))
        act_prev = self.act_closing(self.prev, self.prev_end)
        self.assertEqual(act_prev, D("4400"))
        self.close_prev_month()
        cash0 = self.cash()
        self.act(r, "unpay")
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, D("0"))
        self.assertEqual(self.cash(), cash0 - D("3000"))
        self.assertEqual(self.act_closing(self.prev, self.prev_end), act_prev)
        self.assertEqual(self.check("unpay"), D("7400"))
        listed = self.payments(r)
        self.assertEqual(sum((D(str(p["amount"])) for p in listed if p["reversal"]), D("0")), D("-3000"))
        self.assertTrue(all(p["cancelled"] for p in listed if not p["reversal"]))
        # Оплатили заново и снова откатили — откатывается только новое.
        self.pay_api(r, 500)
        cash1 = self.cash()
        self.act(r, "unpay")
        self.assertEqual(self.cash(), cash1 - D("500"))
        self.assertEqual(self.act_closing(self.prev, self.prev_end), act_prev)
        self.check("unpay twice")


class OverviewReceivedTests(MoneyCase):
    """«Получено» по способам в «Обзоре» считает живую часть оплат: отменённая
    MBank-оплата (запись осталась, рядом встречная) не съедает потолок заказа."""

    def test_cancelled_payment_of_another_method_is_not_received(self):
        from finance.reports.overview import dashboard

        r = self.sale(1000, paid=0)
        self.pay_api(r, 300, method="MBANK")
        self.cancel(r, Payment.objects.get(receipt=r, method="MBANK"))
        self.pay_api(r, 200)
        got = dashboard(self.m0, self.today)["revenue"]["received"]
        self.assertEqual((D(str(got["cash"])), D(str(got["mbank"]))), (D("200"), D("0")))
        # Откат всей оплаты и новая оплата: откат «оплаты при оформлении» тоже
        # не считается полученным.
        r2 = self.sale(1000, paid=400)
        self.act(r2, "unpay")
        self.pay_api(r2, 100, method="MBANK")
        got = dashboard(self.m0, self.today)["revenue"]["received"]
        self.assertEqual((D(str(got["cash"])), D(str(got["mbank"]))), (D("200"), D("100")))


class ReferralAfterCancelledWriteOffTests(MoneyCase):
    """Списание отменено (запись осталась со встречной) и заказ оплачен — для
    реферального бонуса это оплаченный заказ, а не «закрытый списанием»."""

    def test_bonus_after_cancelled_write_off_and_payment(self):
        from clients.models import Client, ReferralBonus
        from finance.models import FinanceSettings

        s = FinanceSettings.load()
        s.referral_bonus = D("500")
        s.save()
        boss = Client.objects.create(full_name="Бакыт", phone="+996700000001")
        x = Client.objects.create(full_name="Х", phone="+996700000002", referred_by=boss)
        r = self.sale(1000, client=x, paid=0)
        self.write_off(r)
        self.assertFalse(ReferralBonus.objects.filter(voided_at__isnull=True).exists())
        self.cancel(r, Payment.objects.get(receipt=r, method="WRITE_OFF"))
        self.pay_api(r, 1000)
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True, referred=x).count(), 1)
        # Частично списанный заказ — по-прежнему не оплаченный.
        r2 = self.sale(1000, client=boss, paid=0)
        self.write_off(r2, amount=100)
        self.pay_api(r2, 900)
        r2.refresh_from_db()
        self.assertEqual(r2.payment_status, Receipt.PaymentStatus.PAID)
        from clients.referrals import qualifying_receipt
        self.assertIsNone(qualifying_receipt(boss))


class ChainTests(MoneyCase):
    """Цепочка: списание → частичный возврат → отмена оплаты → удаление."""

    def test_chain(self):
        cash0 = self.cash()
        a = self.sale(10000, paid=0, on=self.on_prev(5))
        self.pay_api(a, 2000, on=self.on_prev(8))
        b = self.sale(5000, paid=1000, on=self.on_prev(12))
        self.pay_api(b, 1500, on=self.on_prev(20))
        self.check("старт")

        # 1. Списание: часть долга A и часть долга B.
        self.write_off(a, amount=5000, on=self.on_prev(25))
        self.write_off(b, amount=500)
        self.assertEqual(self.bad_debt(), D("5500"))
        self.assertEqual(self.check("списание"), D("3000") + D("2000"))
        act_prev = self.act_closing(self.prev, self.prev_end)
        self.close_prev_month()

        # 2. Частичный возврат A на 6 000: 3 000 гасят долг, 3 000 — списание.
        self.refund(a, qty=6000)
        a.refresh_from_db()
        self.assertEqual(self.cash(), cash0 + D("4500"))
        self.assertEqual(writeoff_total(a), D("2000"))
        self.assertEqual(self.bad_debt(self.prev, self.prev_end), D("5000"))   # закрытый месяц цел
        self.assertEqual(self.bad_debt(), D("2500"))
        self.assertEqual(self.act_closing(self.prev, self.prev_end), act_prev)
        self.assertEqual(self.check("возврат"), D("2000"))

        # 3. Отмена оплаты: у возвращённого A — нельзя, у B — оплата и списание.
        self.cancel(a, Payment.objects.get(receipt=a, method="CASH"), expect=400)
        self.cancel(b, Payment.objects.get(receipt=b, method="CASH"))
        self.cancel(b, Payment.objects.get(receipt=b, method="WRITE_OFF"))
        self.assertEqual(self.cash(), cash0 + D("3000"))
        self.assertEqual(self.bad_debt(), D("2000"))
        self.assertEqual(self.act_closing(self.prev, self.prev_end), act_prev)
        self.assertEqual(self.check("отмена"), D("4000"))

        # 4. Удаление. Списание A — в закрытом месяце: удалить нельзя, пока он
        #    закрыт (заказ тоже закрытого месяца). B — тоже закрытого месяца.
        self.delete(a, expect=400)
        lock = PeriodLock.load()
        lock.closed_through = None
        lock.save()
        self.delete(a)
        self.delete(b)
        self.assertEqual(self.bad_debt(), D("0"))
        self.assertEqual(self.cash(), cash0)
        self.assertEqual(self.check("удаление"), D("0"))
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.profit(self.prev, month_end(self.today)), D("0"))


class EditAfterWriteOffTests(MoneyCase):
    """Правка состава после списания (найдено при правке RM-N1, D-159): лишнее
    сверх нового итога забирает списание, а не превращается в сдачу."""

    def test_shrinking_a_written_off_order_reduces_the_write_off_not_change(self):
        r = self.sale(7400, paid=0)
        self.write_off(r)
        cash0 = self.cash()
        item = r.items.get()
        out = self.client.post(
            f"/api/sales/receipts/{r.id}/edit-items/",
            {"items": [{"id": item.id, "quantity": "3700"}]}, format="json",
        )
        self.assertEqual(out.status_code, 200, getattr(out, "data", out))
        r.refresh_from_db()
        self.assertEqual(r.change_due, D("0"))
        self.assertEqual(r.debt, D("0"))
        self.assertEqual(writeoff_total(r), D("3700"))
        self.assertEqual(self.bad_debt(), D("3700"))
        self.assertEqual(self.cash(), cash0)
        self.check("edit after write-off")

    def test_shrinking_a_partly_paid_written_off_order(self):
        r = self.sale(7400, paid=1000)
        self.write_off(r)                       # списано 6 400
        item = r.items.get()
        out = self.client.post(
            f"/api/sales/receipts/{r.id}/edit-items/",
            {"items": [{"id": item.id, "quantity": "700"}]}, format="json",
        )
        self.assertEqual(out.status_code, 200, getattr(out, "data", out))
        r.refresh_from_db()
        # Лишние 6 700: 6 400 забирает списание целиком, 300 — сдача из реальных денег.
        self.assertEqual(writeoff_total(r), D("0"))
        self.assertEqual(self.bad_debt(), D("0"))
        self.assertEqual(r.change_due, D("300"))
        self.assertEqual(r.debt, D("0"))
        self.check("edit partly paid")
