"""S3 перепроверки владельца: возврат.

- RM-N10: вернуть количество больше, чем осталось в строке, — 400, а не
  «молча вся строка»;
- RM-N7: заказ, оплаченный авансом, возвращается в аванс (по умолчанию —
  туда, откуда пришли деньги); способ «на аванс клиента» (`ADVANCE`) кладёт
  и деньги кассы в аванс: касса не двигается, аванс растёт.
"""
from decimal import Decimal as D

from django.utils import timezone

from clients.advances import accept_advance, advance_available, revert_advance
from clients.models import BalanceOffset, ClientAdvance
from clients.statement import build_statement
from finance.models import CashEntry
from finance.reports.bridge import bridge
from sales.models import Receipt
from sales.tests_cash_ops import CashOpsBase


class RefundQuantityAboveLineTests(CashOpsBase):
    def test_more_than_the_line_is_a_400(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        line = r.items.get()
        out = self.post(r, "refund", {"quantities": [{"id": line.id, "quantity": "15"}]})
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn("10", out.data["detail"])
        line.refresh_from_db()
        self.assertFalse(line.is_returned)
        self.assertFalse(CashEntry.objects.filter(receipt=r, article=CashEntry.Article.REFUND).exists())

    def test_exactly_the_line_still_returns_it(self):
        r = self.sale(10, paid=D("1000"))
        line = r.items.get()
        out = self.post(r, "refund", {"quantities": [{"id": line.id, "quantity": "10"}]})
        self.assertEqual(out.status_code, 200, out.data)
        line.refresh_from_db()
        self.assertTrue(line.is_returned)


class AdvanceRefundTests(CashOpsBase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        accept_advance(self.ivan, D("5000"), method="CASH", user=self.admin)
        self.cash0 = CashEntry.balance("CASH")

    def unexplained(self):
        return bridge(self.today.replace(day=1), self.today)["unexplained"]

    def closing(self):
        return D(str(build_statement(self.ivan)["closing"]))

    def by_advance(self, n, paid=D("0")):
        r = self.sale(n, paid=paid, client=self.ivan, use_advance=True)
        r.refresh_from_db()
        return r

    def test_full_refund_of_an_advance_order_goes_back_to_the_advance(self):
        r = self.by_advance(37)                                   # 3 700 авансом
        self.assertEqual(advance_available(self.ivan), D("1300"))
        out = self.post(r, "refund", {"reason": "не подошло"})
        self.assertEqual(out.status_code, 200, out.data)
        r.refresh_from_db()
        self.assertEqual(advance_available(self.ivan), D("5000"))  # аванс снова 5 000
        self.assertEqual(CashEntry.balance("CASH"), self.cash0)   # касса та же
        self.assertFalse(CashEntry.objects.filter(receipt=r, article=CashEntry.Article.REFUND).exists())
        self.assertEqual((r.amount_paid, r.change_applied, r.debt), (D("0"), D("0"), D("0")))
        self.assertEqual(self.closing(), D("-5000"))
        self.assertEqual(self.unexplained(), D("0"))

    def test_partial_refund_returns_its_share_to_the_advance(self):
        r = self.by_advance(37)
        line = r.items.get()
        out = self.post(r, "refund", {"quantities": [{"id": line.id, "quantity": "10"}]})
        self.assertEqual(out.status_code, 200, out.data)
        r.refresh_from_db()
        self.assertEqual(advance_available(self.ivan), D("2300"))  # 1 300 + 1 000
        self.assertEqual((r.amount_paid, r.change_applied, r.debt), (D("2700"), D("2700"), D("0")))
        self.assertEqual(
            sum(o.amount for o in BalanceOffset.objects.filter(receipt=r, source="ADVANCE")), D("2700"),
        )
        self.assertEqual(CashEntry.balance("CASH"), self.cash0)
        self.assertEqual(self.closing(), D("-2300"))
        self.assertEqual(self.unexplained(), D("0"))

    def test_mixed_payment_advance_back_first_then_cash(self):
        r = self.by_advance(37, paid=D("1000"))                   # 1 000 деньгами + 2 700 авансом
        self.assertEqual(advance_available(self.ivan), D("2300"))
        cash1 = CashEntry.balance("CASH")
        out = self.post(r, "refund")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(advance_available(self.ivan), D("5000"))
        self.assertEqual(CashEntry.balance("CASH"), cash1 - D("1000"))
        self.assertEqual(self.closing(), D("-5000"))
        self.assertEqual(self.unexplained(), D("0"))

    def test_explicit_cash_still_pays_everything_out(self):
        r = self.by_advance(37)
        out = self.post(r, "refund", {"method": "CASH"})
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(advance_available(self.ivan), D("1300"))
        self.assertEqual(CashEntry.balance("CASH"), self.cash0 - D("3700"))

    def test_cash_order_refunded_to_the_advance(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        cash1 = CashEntry.balance("CASH")
        out = self.post(r, "refund", {"method": "ADVANCE"})
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(CashEntry.balance("CASH"), cash1)        # деньги из кассы не ушли
        self.assertEqual(advance_available(self.ivan), D("6000"))
        made = ClientAdvance.objects.filter(client=self.ivan).order_by("-id").first()
        self.assertEqual(made.amount, D("1000"))
        self.assertIn(str(r.order_number), made.note)
        self.assertEqual(self.closing(), D("-6000"))
        self.assertEqual(self.unexplained(), D("0"))

    def test_advance_needs_a_client(self):
        r = self.sale(10, paid=D("1000"))
        out = self.post(r, "refund", {"method": "ADVANCE"})
        self.assertEqual(out.status_code, 400, out.data)
        self.assertFalse(Receipt.objects.get(pk=r.pk).items.filter(is_returned=True).exists())

    def test_undo_of_a_refund_into_the_advance_waits_for_the_advance(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)
        cash1 = CashEntry.balance("CASH")
        self.assertEqual(self.post(r, "refund", {"method": "ADVANCE"}).status_code, 200)
        line = r.items.get()
        blocked = self.post(r, "undo-refund", {"item_ids": [line.id]})
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertIn("аванс", blocked.data["detail"])
        made = ClientAdvance.objects.filter(client=self.ivan).order_by("-id").first()
        revert_advance(made, user=self.admin)
        ok = self.post(r, "undo-refund", {"item_ids": [line.id]})
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(CashEntry.balance("CASH"), cash1)
        self.assertEqual(advance_available(self.ivan), D("5000"))
        self.assertEqual(self.unexplained(), D("0"))
