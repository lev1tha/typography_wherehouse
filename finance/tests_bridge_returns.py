"""Сверка «прибыль → поток»: заказ, оплаченный в одном месяце и целиком
возвращённый в другом, не оставляет «Не объяснено» (цель D-11: всегда 0).

Раньше такой заказ (статус «Отменён») выпадал из позиций клиентов целиком, хотя
в месяце продажи он был и выручкой, и деньгами: сентябрь показывал «−3 000
не объяснено», октябрь — «+3 000».
"""
from datetime import date
from decimal import Decimal

from finance.models import CashEntry
from finance.periods import month_end
from finance.reports import bridge as bridge_mod
from finance.tests_reports_calc import PnlCase, noon
from sales import sale_service
from sales.models import TransactionItem

D = Decimal


class ReturnedInAnotherMonthTests(PnlCase):
    def unexplained(self, first, last):
        return bridge_mod.bridge(first, last)["unexplained"]

    def refund(self, receipt, day, item_ids=None):
        sale_service.refund_receipt(receipt, item_ids=item_ids, user=self.admin)
        TransactionItem.objects.filter(receipt=receipt, is_returned=True).update(
            returned_at=noon(day)
        )
        CashEntry.objects.filter(article=CashEntry.Article.REFUND, receipt=receipt).update(
            happened_on=day
        )

    def test_fully_returned_next_month(self):
        receipt = self.sale(date(2026, 9, 15), qty=10, paid="3000")      # 3 000 оплачено
        self.refund(receipt, date(2026, 10, 5))                          # вернули целиком
        for first, last in (
            (date(2026, 9, 1), date(2026, 9, 30)),
            (date(2026, 10, 1), date(2026, 10, 31)),
            (date(2026, 9, 1), date(2026, 10, 31)),
            (date(2026, 9, 20), date(2026, 10, 3)),
        ):
            with self.subTest(period=(first, last)):
                self.assertEqual(self.unexplained(first, last), D("0"))

    def test_partial_return_stays_zero(self):
        """Две строки в заказе, одна возвращена в следующем месяце."""
        receipt = sale_service.create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[
                {"type": "MATERIAL", "material": self.plate, "quantity": D(4), "mode": "PIECE"},
                {"type": "MATERIAL", "material": self.plate, "quantity": D(6), "mode": "PIECE"},
            ],
            amount_paid=D("3000"), created_at=noon(date(2026, 9, 15)),
        )
        first = receipt.items.order_by("id").first()
        self.refund(receipt, date(2026, 10, 5), item_ids=[first.id])
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, "PARTIALLY_REFUNDED")
        for first_day, last in ((date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31))):
            self.assertEqual(self.unexplained(first_day, last), D("0"))

    def test_return_in_the_same_month_is_zero(self):
        receipt = self.sale(date(2026, 9, 15), qty=10, paid="3000")
        self.refund(receipt, date(2026, 9, 18))
        self.assertEqual(self.unexplained(date(2026, 9, 1), date(2026, 9, 30)), D("0"))
        self.assertEqual(self.unexplained(date(2026, 10, 1), date(2026, 10, 31)), D("0"))

    def test_unpaid_order_returned_later_is_zero(self):
        receipt = self.sale(date(2026, 9, 15), qty=10, paid="0")
        self.refund(receipt, date(2026, 10, 5))
        for first, last in ((date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31))):
            self.assertEqual(self.unexplained(first, last), D("0"))


class BridgeFuzzTests(PnlCase):
    """Случайные заказы, оплаты и возвраты за четыре месяца: «Не объяснено» = 0
    в каждом месяце и за весь период (цель D-11)."""

    SEEDS = range(25)

    def scenario(self, rng):
        days = [date(2026, m, d) for m in (8, 9, 10, 11) for d in (3, 12, 21, 28)]
        orders = []
        for _ in range(rng.randint(3, 7)):
            day = rng.choice(days)
            lines = [
                {"type": "MATERIAL", "material": self.plate, "quantity": D(rng.randint(1, 6)), "mode": "PIECE"}
                for _ in range(rng.randint(1, 3))
            ]
            total = sum(l["quantity"] for l in lines) * D(300)
            paid = rng.choice([D(0), total, total, total / 2, total + D(100)])
            receipt = sale_service.create_sale(
                client=None, cashier=self.admin, payment_method="CASH",
                items_data=lines, amount_paid=paid, created_at=noon(day),
            )
            orders.append((receipt, day))
        for receipt, day in orders:
            receipt.refresh_from_db()
            later = [d for d in days if d > day]
            if receipt.debt > 0 and later and rng.random() < 0.5:
                sale_service.apply_payment(
                    receipt, receipt.debt, user=self.admin, paid_on=rng.choice(later)
                )
            if later and rng.random() < 0.6:
                back_on = rng.choice(later)
                ids = None
                if receipt.items.count() > 1 and rng.random() < 0.5:
                    ids = [receipt.items.order_by("id").first().id]
                sale_service.refund_receipt(receipt, item_ids=ids, user=self.admin)
                TransactionItem.objects.filter(receipt=receipt, is_returned=True).update(
                    returned_at=noon(back_on)
                )
                CashEntry.objects.filter(
                    article=CashEntry.Article.REFUND, receipt=receipt
                ).update(happened_on=back_on)

    def test_nothing_unexplained_in_any_month(self):
        import random

        from django.db import transaction

        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                with transaction.atomic():
                    self.scenario(rng)
                    for m in (8, 9, 10, 11):
                        first = date(2026, m, 1)
                        last = month_end(first)
                        b = bridge_mod.bridge(first, last)
                        self.assertEqual(b["unexplained"], D("0"), f"месяц {m}")
                    b = bridge_mod.bridge(date(2026, 8, 1), date(2026, 11, 30))
                    self.assertEqual(b["unexplained"], D("0"), "весь период")
                    transaction.set_rollback(True)
