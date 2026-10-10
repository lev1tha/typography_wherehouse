"""Касса и долги: счёт сдачи и возврата, отмена одной оплаты, перенос даты,
зачёт сдачи, списание долга, отмена возврата, складовщик принимает долг
(2026-10-10, cash-02, -03, -04, -08, -09, -10, CLI-08, STAFF-08).
"""
from datetime import timedelta
from decimal import Decimal as D

from audit.models import AuditLog
from finance import cash
from finance.models import CashEntry, ExpenseEntry, ExpenseKind, PeriodLock
from sales.models import Payment, Receipt
from sales.sale_service import create_sale, pay_client_debt
from sales.tests_calc_base import CalcBase, RECEIPTS
from warehouse.models import Material


def _sum(qs):
    return sum((e.amount for e in qs), D("0"))


class CashOpsBase(CalcBase):
    def setUp(self):
        super().setUp()
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=D("1000"),
            price_per_unit=D("100"), piece_price=D("100"), purchase_price=D("40"),
        )

    def sale(self, qty, *, paid=None, client=None, method="CASH", user=None, **kw):
        """Заказ из `qty` штук крепежа по 100 сом."""
        return create_sale(
            client=client, cashier=user or self.admin, payment_method=method,
            items_data=[{"type": "MATERIAL", "material": self.bolts, "quantity": qty, "mode": "PIECE"}],
            amount_paid=paid, **kw,
        )

    def post(self, receipt, action, body=None, user=None):
        self.client.force_authenticate(user or self.admin)
        return self.client.post(f"{RECEIPTS}{receipt.id}/{action}/", body or {}, format="json")


class AccountChoiceTests(CashOpsBase):
    def test_change_is_given_from_cash_by_default_and_from_bank_on_request(self):
        r = self.sale(15, paid=D("3000"), client=self.ivan)           # заказ 1500, сдача 1500
        self.assertEqual(r.change_due, D("1500"))
        a = self.post(r, "give-change", {"amount": "500"})
        self.assertEqual(a.status_code, 200, a.data)
        out = CashEntry.objects.filter(receipt=r, article=CashEntry.Article.CHANGE).get()
        self.assertEqual(out.account, CashEntry.Account.CASH)
        b = self.post(r, "give-change", {"amount": "500", "method": "MBANK"})
        self.assertEqual(b.status_code, 200, b.data)
        banked = CashEntry.objects.filter(receipt=r, article=CashEntry.Article.CHANGE).order_by("-id").first()
        self.assertEqual(banked.account, cash.account_for("MBANK"))
        self.assertEqual(banked.amount, D("500"))

    def test_unknown_change_method_is_a_400(self):
        r = self.sale(15, paid=D("3000"), client=self.ivan)
        self.assertEqual(self.post(r, "give-change", {"method": "bitcoin"}).status_code, 400)

    def test_refund_goes_out_from_the_chosen_account(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)           # 1000 наличными
        out = self.post(r, "refund", {"method": "MBANK"})
        self.assertEqual(out.status_code, 200, out.data)
        entry = CashEntry.objects.get(receipt=r, article=CashEntry.Article.REFUND)
        self.assertEqual(entry.account, cash.account_for("MBANK"))
        self.assertEqual(entry.amount, D("1000"))

    def test_refund_default_goes_back_where_the_money_came_from(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        self.assertEqual(self.post(r, "refund").status_code, 200)
        entry = CashEntry.objects.get(receipt=r, article=CashEntry.Article.REFUND)
        self.assertEqual(entry.account, CashEntry.Account.CASH)

    def test_cash_for_an_online_order_goes_to_the_cash_drawer(self):
        r = self.sale(10, client=self.ivan, method="ONLINE")
        out = self.post(r, "pay", {"amount": "1000"})
        self.assertEqual(out.status_code, 200, out.data)
        payment = Payment.objects.get(receipt=r)
        self.assertEqual(payment.method, "CASH")
        entry = CashEntry.objects.get(receipt=r, article=CashEntry.Article.SALE)
        self.assertEqual(entry.account, CashEntry.Account.CASH)

    def test_online_order_can_still_be_paid_by_bank_explicitly(self):
        r = self.sale(10, client=self.ivan, method="ONLINE")
        self.post(r, "pay", {"amount": "1000", "method": "MBANK"})
        entry = CashEntry.objects.get(receipt=r, article=CashEntry.Article.SALE)
        self.assertEqual(entry.account, cash.account_for("MBANK"))


class CancelOnePaymentTests(CashOpsBase):
    def _debt_with_two_payments(self):
        r = self.sale(10, paid=D("0"), client=self.ivan)
        self.post(r, "pay", {"amount": "300"})
        self.post(r, "pay", {"amount": "200"})
        return r

    def test_cancel_removes_only_that_payment_and_keeps_the_book(self):
        r = self._debt_with_two_payments()
        first = Payment.objects.filter(receipt=r).order_by("id").first()
        out = self.post(r, "cancel-payment", {"payment": first.id, "reason": "ошибся счётом"})
        self.assertEqual(out.status_code, 200, out.data)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, D("200"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PENDING)
        # Отменённая оплата остаётся своим днём, отмена — встречной записью с
        # минусом (D-158): живых оплат одна, сумма записей = принятому после.
        self.assertEqual(Payment.objects.filter(receipt=r).count(), 3)
        self.assertEqual(_sum(Payment.objects.filter(receipt=r)), D("200"))
        self.assertEqual(Payment.objects.get(receipt=r, amount__lt=0).amount, D("-300"))
        # Исходный приход остался в книге, встречная запись — рядом.
        self.assertEqual(_sum(CashEntry.objects.filter(receipt=r, article="SALE", kind="IN")), D("500"))
        back = CashEntry.objects.get(receipt=r, article=CashEntry.Article.UNPAY)
        self.assertEqual((back.kind, back.amount), ("OUT", D("300")))
        self.assertTrue(AuditLog.objects.filter(action__contains="Отмена оплаты").exists())
        self.assertEqual(sum(cash.held_by_account(r).values(), D("0")), D("200"))

    def test_cancelling_a_closing_payment_reopens_the_debt(self):
        r = self.sale(10, paid=D("0"), client=self.ivan)
        self.post(r, "pay", {})
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)
        self.post(r, "cancel-payment", {"payment": Payment.objects.get(receipt=r).id})
        r.refresh_from_db()
        self.assertEqual(r.debt, D("1000"))

    def test_lock_is_by_today_not_by_the_order_date(self):
        old = self.today - timedelta(days=40)
        r = self.sale(10, paid=D("0"), client=self.ivan, created_at=_noon(old))
        self.post(r, "pay", {"amount": "400"})
        PeriodLock.load()
        lock = PeriodLock.load()
        lock.closed_through = self.today - timedelta(days=10)
        lock.save()
        pay = Payment.objects.get(receipt=r)
        out = self.post(r, "cancel-payment", {"payment": pay.id})
        self.assertEqual(out.status_code, 200, out.data)       # заказ в закрытом месяце, а сегодня открыто

    def test_unpay_of_an_old_order_is_allowed_today(self):
        old = self.today - timedelta(days=40)
        r = self.sale(10, paid=D("1000"), client=self.ivan, created_at=_noon(old))
        lock = PeriodLock.load()
        lock.closed_through = self.today - timedelta(days=10)
        lock.save()
        self.assertEqual(self.post(r, "unpay").status_code, 200)

    def test_foreign_payment_and_storekeeper_are_refused(self):
        r = self._debt_with_two_payments()
        other = self.sale(10, paid=D("0"), client=self.ivan)
        self.post(other, "pay", {"amount": "100"})
        foreign = Payment.objects.get(receipt=other)
        self.assertEqual(self.post(r, "cancel-payment", {"payment": foreign.id}).status_code, 400)
        mine = Payment.objects.filter(receipt=r).first()
        self.assertEqual(self.post(r, "cancel-payment", {"payment": mine.id}, user=self.store).status_code, 403)

    def test_overpay_three_times_the_debt_asks_for_confirmation(self):
        r = self.sale(1, paid=D("0"), client=self.ivan)             # долг 100
        out = self.post(r, "pay", {"amount": "500"})
        self.assertEqual(out.status_code, 409, out.data)
        self.assertEqual(out.data["warnings"][0]["code"], "overpay")
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, D("0"))
        ok = self.post(r, "pay", {"amount": "500", "confirm_overpay": True})
        self.assertEqual(ok.status_code, 200, ok.data)
        r.refresh_from_db()
        self.assertEqual(r.change_due, D("400"))
        # В три раза ровно — ещё не «лишний ноль».
        r2 = self.sale(1, paid=D("0"), client=self.ivan)
        self.assertEqual(self.post(r2, "pay", {"amount": "300"}).status_code, 200)


def _noon(day):
    from sales.sale_service import day_to_moment
    return day_to_moment(day)


class MoveOrderDateMovesCashTests(CashOpsBase):
    def test_first_payment_follows_the_order_date(self):
        three = self.today - timedelta(days=3)
        r = self.sale(10, paid=D("1000"), client=self.ivan, created_at=_noon(three))
        entry = CashEntry.objects.get(receipt=r, article=CashEntry.Article.SALE)
        self.assertEqual(entry.happened_on, three)
        two = self.today - timedelta(days=2)
        out = self.client.patch(f"{RECEIPTS}{r.id}/", {"order_date": two.isoformat()}, format="json") \
            if self.client.force_authenticate(self.admin) is None else None
        self.assertEqual(out.status_code, 200, out.data)
        entry.refresh_from_db()
        self.assertEqual(entry.happened_on, two)
        self.assertTrue(AuditLog.objects.filter(action__contains="деньги первой оплаты").exists())

    def test_a_later_payment_stays_where_it_was(self):
        three = self.today - timedelta(days=3)
        r = self.sale(10, paid=D("300"), client=self.ivan, created_at=_noon(three))
        self.post(r, "pay", {"amount": "200", "paid_on": (self.today - timedelta(days=1)).isoformat()})
        self.client.patch(f"{RECEIPTS}{r.id}/", {"order_date": (self.today - timedelta(days=2)).isoformat()},
                          format="json")
        days = sorted(CashEntry.objects.filter(receipt=r, article="SALE").values_list("happened_on", flat=True))
        self.assertEqual(days, [self.today - timedelta(days=2), self.today - timedelta(days=1)])

    def test_first_payment_made_on_another_day_is_not_moved(self):
        three = self.today - timedelta(days=3)
        r = self.sale(10, paid=D("0"), client=self.ivan, created_at=_noon(three))
        self.post(r, "pay", {"amount": "1000"})                  # деньги принесли сегодня
        self.client.patch(f"{RECEIPTS}{r.id}/", {"order_date": (self.today - timedelta(days=2)).isoformat()},
                          format="json")
        entry = CashEntry.objects.get(receipt=r, article="SALE")
        self.assertEqual(entry.happened_on, self.today)


class UseChangeOnPayTests(CashOpsBase):
    def _setup(self):
        a = self.sale(15, paid=D("2000"), client=self.ivan)          # заказ 1500, сдача 500
        b = self.sale(10, paid=D("0"), client=self.ivan)             # долг 1000
        return a, b

    def test_change_covers_the_debt_before_cash(self):
        a, b = self._setup()
        out = self.post(b, "pay", {"amount": "500", "use_change": True})
        self.assertEqual(out.status_code, 200, out.data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.change_due, D("0"))
        self.assertEqual((b.amount_paid, b.change_applied), (D("1000"), D("500")))
        self.assertEqual(b.payment_status, Receipt.PaymentStatus.PAID)
        methods = sorted(Payment.objects.filter(receipt=b).values_list("method", flat=True))
        self.assertEqual(methods, ["CASH", "CHANGE"])
        # В кассу легли только принесённые 500: сдача уже лежала с того раза.
        self.assertEqual(_sum(CashEntry.objects.filter(receipt=b, article="SALE")), D("500"))

    def test_without_amount_the_rest_is_cash(self):
        a, b = self._setup()
        self.post(b, "pay", {"use_change": True})
        b.refresh_from_db()
        self.assertEqual(b.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(b.change_applied, D("500"))
        self.assertEqual(_sum(CashEntry.objects.filter(receipt=b, article="SALE")), D("500"))

    def test_change_is_taken_only_for_what_cash_does_not_cover(self):
        a, b = self._setup()
        self.post(b, "pay", {"amount": "900", "use_change": True})
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(b.change_applied, D("100"))
        self.assertEqual(a.change_due, D("400"))

    def test_cancelling_the_change_payment_gives_the_change_back(self):
        a, b = self._setup()
        self.post(b, "pay", {"amount": "500", "use_change": True})
        change_payment = Payment.objects.get(receipt=b, method="CHANGE")
        self.assertEqual(self.post(b, "cancel-payment", {"payment": change_payment.id}).status_code, 200)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.change_due, D("500"))
        self.assertEqual((b.amount_paid, b.change_applied), (D("500"), D("0")))


class StorekeeperAcceptsDebtTests(CashOpsBase):
    def test_storekeeper_takes_a_debt_payment_and_is_recorded(self):
        r = self.sale(10, paid=D("0"), client=self.ivan)
        out = self.post(r, "pay", {"amount": "400"}, user=self.store)
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(out.data["payments"][0]["created_by_name"], "c_store")
        self.assertEqual(Payment.objects.get(receipt=r).created_by, self.store)

    def test_storekeeper_cannot_backdate_or_write_off(self):
        r = self.sale(10, paid=D("0"), client=self.ivan)
        back = self.post(r, "pay", {"amount": "100", "paid_on": (self.today - timedelta(days=1)).isoformat()},
                         user=self.store)
        self.assertEqual(back.status_code, 403, back.data)
        wo = self.post(r, "pay", {"amount": "100", "method": "WRITE_OFF"}, user=self.store)
        self.assertEqual(wo.status_code, 403, wo.data)

    def test_the_owner_can_switch_it_off_for_both_paths(self):
        from clients.models import ClientSettings

        ClientSettings.objects.update_or_create(pk=1, defaults={"storekeeper_takes_debt": False})
        r = self.sale(10, paid=D("0"), client=self.ivan)
        self.assertEqual(self.post(r, "pay", {"amount": "400"}, user=self.store).status_code, 403)
        self.assertEqual(self.post(r, "pay", {"amount": "400"}).status_code, 200)       # админ — всегда

    def test_accountant_still_cannot_take_money(self):
        r = self.sale(10, paid=D("0"), client=self.ivan)
        self.assertEqual(self.post(r, "pay", {"amount": "100"}, user=self.accountant).status_code, 403)

    def test_admin_can_cancel_what_the_storekeeper_took(self):
        r = self.sale(10, paid=D("0"), client=self.ivan)
        self.post(r, "pay", {"amount": "400"}, user=self.store)
        pay = Payment.objects.get(receipt=r)
        self.assertEqual(self.post(r, "cancel-payment", {"payment": pay.id}).status_code, 200)


class WriteOffTests(CashOpsBase):
    def _debt(self):
        return self.sale(10, paid=D("0"), client=self.ivan)         # долг 1000

    def test_write_off_reduces_the_debt_without_cash_and_creates_the_expense(self):
        r = self._debt()
        cash_before = CashEntry.objects.count()
        out = self.post(r, "write-off", {"amount": "400", "note": "клиент пропал"})
        self.assertEqual(out.status_code, 200, out.data)
        r.refresh_from_db()
        self.assertEqual((r.amount_paid, r.debt), (D("400"), D("600")))
        self.assertEqual(CashEntry.objects.count(), cash_before)            # денег нет
        kind = ExpenseKind.objects.get(code="BAD_DEBT")
        expense = ExpenseEntry.objects.get(kind=kind)
        self.assertEqual(expense.amount, D("400"))
        self.assertEqual(expense.created_by, self.admin)
        payment = Payment.objects.get(receipt=r)
        self.assertEqual((payment.method, payment.expense_id), ("WRITE_OFF", expense.pk))
        self.assertEqual(CashEntry.objects.filter(expense=expense).count(), 0)

    def test_write_off_everything_closes_the_order(self):
        r = self._debt()
        self.post(r, "write-off", {"note": "безнадёжный"})
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(r.debt, D("0"))

    def test_reason_is_required_and_only_admin_may_do_it(self):
        r = self._debt()
        self.assertEqual(self.post(r, "write-off", {"amount": "10"}).status_code, 400)
        self.assertEqual(self.post(r, "write-off", {"note": "x"}, user=self.store).status_code, 403)

    def test_cancelling_the_write_off_removes_its_expense(self):
        r = self._debt()
        self.post(r, "write-off", {"amount": "400", "note": "ошибка"})
        pay = Payment.objects.get(receipt=r)
        self.assertEqual(self.post(r, "cancel-payment", {"payment": pay.id}).status_code, 200)
        self.assertFalse(ExpenseEntry.objects.filter(kind__code="BAD_DEBT").exists())
        r.refresh_from_db()
        self.assertEqual(r.debt, D("1000"))
        self.assertFalse(CashEntry.objects.filter(article="UNPAY").exists())   # денег не было — откатывать нечего

    def test_unpay_after_a_write_off_does_not_pull_cash_that_never_came(self):
        r = self.sale(10, paid=D("300"), client=self.ivan)
        self.post(r, "write-off", {"amount": "200", "note": "x"})
        self.assertEqual(self.post(r, "unpay").status_code, 200)
        self.assertFalse(ExpenseEntry.objects.filter(kind__code="BAD_DEBT").exists())
        out = _sum(CashEntry.objects.filter(receipt=r, article="UNPAY"))
        self.assertEqual(out, D("300"))

    def test_pay_client_debt_accepts_write_off_for_several_orders(self):
        self._debt()
        self._debt()
        allocations, change = pay_client_debt(
            self.ivan, None, user=self.admin, method="WRITE_OFF", note="списали обоих",
        )
        self.assertEqual(len(allocations), 2)
        self.assertEqual(change, D("0"))
        self.assertEqual(_sum(ExpenseEntry.objects.filter(kind__code="BAD_DEBT")), D("2000"))
        self.assertEqual(CashEntry.objects.filter(article="SALE").count(), 0)

    def test_pay_client_debt_write_off_is_admin_only(self):
        from sales.sale_service import PaymentRejected
        self._debt()
        with self.assertRaises(PaymentRejected):
            pay_client_debt(self.ivan, None, user=self.store, method="WRITE_OFF")

    def test_write_off_is_not_a_receipt_payment_method(self):
        out = self.co([{"type": "MATERIAL", "material": self.bolts.id, "mode": "PIECE", "quantity": 1}],
                      payment_method="WRITE_OFF")
        self.assertEqual(out.status_code, 400)


class RefundExtrasTests(CashOpsBase):
    def test_quantity_in_a_refund_is_a_400_not_a_whole_line_refund(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        line = r.items.get()
        out = self.post(r, "refund", {"item_ids": [line.id], "quantity": 3})
        self.assertEqual(out.status_code, 400, out.data)
        r.refresh_from_db()
        self.assertEqual(r.refunded_amount, D("0"))

    def test_storekeeper_needs_a_reason_to_refund_a_paid_order(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        no = self.post(r, "refund", {}, user=self.store)
        self.assertEqual(no.status_code, 400, no.data)
        self.assertIn("reason", no.data)
        yes = self.post(r, "refund", {"reason": "клиент передумал"}, user=self.store)
        self.assertEqual(yes.status_code, 200, yes.data)
        self.assertTrue(AuditLog.objects.filter(action__contains="клиент передумал").exists())

    def test_unpaid_order_needs_no_reason_and_admin_never_does(self):
        unpaid = self.sale(10, paid=D("0"), client=self.ivan)
        self.assertEqual(self.post(unpaid, "refund", {}, user=self.store).status_code, 200)
        paid = self.sale(10, paid=D("1000"), client=self.ivan)
        self.assertEqual(self.post(paid, "refund", {}, user=self.admin).status_code, 200)

    def test_undo_refund_puts_everything_back(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        self.post(r, "refund", {})
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("1000"))
        out = self.post(r, "undo-refund", {})
        self.assertEqual(out.status_code, 200, out.data)
        r.refresh_from_db()
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("990"))                 # снова продано
        self.assertEqual((r.refunded_amount, r.status, r.payment_status),
                         (D("0"), Receipt.Status.COMPLETED, Receipt.PaymentStatus.PAID))
        self.assertFalse(r.items.filter(is_returned=True).exists())
        entries = CashEntry.objects.filter(receipt=r, article=CashEntry.Article.REFUND)
        self.assertEqual(_sum(entries.filter(kind="OUT")), _sum(entries.filter(kind="IN")))
        self.assertEqual(sum(cash.held_by_account(r).values(), D("0")), D("1000"))
        self.assertEqual(r.items.get().cost_total, D("400"))

    def test_undo_refund_books_the_stock_movement_today_not_on_the_order_date(self):
        from warehouse.models import InventoryLog

        old = self.today - timedelta(days=20)
        r = self.sale(10, paid=D("1000"), client=self.ivan, created_at=_noon(old))
        self.post(r, "refund", {})
        self.post(r, "undo-refund", {})
        sales = list(InventoryLog.objects.filter(receipt=r, type="SALE").order_by("id"))
        self.assertEqual(len(sales), 2)
        self.assertEqual(sales[0].happened_at.date(), old)                  # исходная продажа
        self.assertEqual(sales[1].happened_at.date(), self.today)           # повторное списание

    def test_undo_refund_of_an_unpaid_order(self):
        r = self.sale(10, paid=D("0"), client=self.ivan)
        self.post(r, "refund", {})
        self.post(r, "undo-refund", {})
        r.refresh_from_db()
        self.assertEqual((r.debt, r.status), (D("1000"), Receipt.Status.COMPLETED))

    def test_undo_refund_is_admin_only_and_needs_a_refund(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        self.assertEqual(self.post(r, "undo-refund", {}).status_code, 400)
        self.post(r, "refund", {})
        self.assertEqual(self.post(r, "undo-refund", {}, user=self.store).status_code, 403)


class DeleteAfterPartialRefundTests(CashOpsBase):
    def test_cannot_delete_an_order_whose_change_went_into_another_order(self):
        glue = Material.objects.create(
            name="Клей", unit=Material.Unit.PIECE, quantity=D("100"),
            price_per_unit=D("500"), piece_price=D("500"), purchase_price=D("200"),
        )
        a = create_sale(
            client=self.ivan, cashier=self.admin, payment_method="CASH",
            items_data=[
                {"type": "MATERIAL", "material": self.bolts, "quantity": 10, "mode": "PIECE"},
                {"type": "MATERIAL", "material": glue, "quantity": 1, "mode": "PIECE"},
            ],
            amount_paid=D("3000"),                                        # заказ 1500, сдача 1500
        )
        self.post(a, "refund", {"item_ids": [a.items.get(material=glue).id]})   # частичный возврат
        a.refresh_from_db()
        self.assertGreater(a.refunded_amount, 0)
        b = self.sale(21, paid=D("500"), client=self.ivan)                   # заказ 2100
        self.post(b, "pay", {"use_change": True})                            # зачли сдачу A в B
        self.client.force_authenticate(self.admin)
        out = self.client.delete(f"{RECEIPTS}{a.id}/")
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn(f"№{b.order_number}", out.data["detail"])
        self.assertTrue(Receipt.objects.filter(pk=a.pk).exists())

    def test_refunded_order_without_spent_change_still_deletes(self):
        a = self.sale(10, paid=D("1000"), client=self.ivan)
        self.post(a, "refund", {"item_ids": [a.items.get().id]})
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f"{RECEIPTS}{a.id}/").status_code, 204)
