"""Частичный возврат КОЛИЧЕСТВА строки (cash-09, волна 2).

Клиент вернул 3 болта из 10 — раньше это был 400 «верните строку целиком».
Теперь часть отделяется в свою строку и возвращается она. Правило округления —
без потери сома: остающаяся часть считается как любая строка (вверх до сома),
возвращаемая получает остаток; итог заказа не меняется.
"""
from datetime import timedelta
from decimal import Decimal as D

from django.utils import timezone

from finance.models import CashEntry
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale, refund_receipt
from sales.tests_cash_ops import CashOpsBase
from services.models import PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class PartialQuantityRefundTests(CashOpsBase):
    def refund(self, receipt, rows, **extra):
        return self.post(receipt, "refund", {"quantities": rows, **extra})

    def test_three_of_ten_bolts(self):
        r = self.sale(10, paid=D("1000"), client=self.ivan)               # 10 × 100
        line = r.items.get()
        stock = Material.objects.get(pk=self.bolts.pk).quantity
        out = self.refund(r, [{"id": line.id, "quantity": "3"}])
        self.assertEqual(out.status_code, 200, out.data)
        r.refresh_from_db()
        self.assertEqual(r.total_price, D("1000"))                        # итог не сдвинулся
        self.assertEqual(r.refunded_amount, D("300"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(r.debt, D("0"))
        kept, back = r.items.order_by("id")
        self.assertEqual((kept.quantity, kept.is_returned), (D("7"), False))
        self.assertEqual((back.quantity, back.is_returned), (D("3"), True))
        self.assertEqual(kept.cost_total + back.cost_total, D("400"))      # 10 × 40
        self.assertEqual(back.cost_total, D("120"))
        self.assertEqual(Material.objects.get(pk=self.bolts.pk).quantity, stock + 3)
        refund_out = CashEntry.objects.get(receipt=r, article=CashEntry.Article.REFUND)
        self.assertEqual(refund_out.amount, D("300"))

    def test_rounding_keeps_the_som(self):
        """3 × 33,33 = 99,99 → 100 сом. Вернули 1: остаётся ⌈2 × 33,33⌉ = 67,
        возвращается 100 − 67 = 33. Сумма та же — 100."""
        r = create_sale(
            client=self.ivan, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.bolts, "quantity": 3, "mode": "PIECE",
                         "material_price": D("33.33")}],
            amount_paid=D("100"),
        )
        self.assertEqual(r.total_price, D("100"))
        line = r.items.get()
        out = self.refund(r, [{"id": line.id, "quantity": "1"}])
        self.assertEqual(out.status_code, 200, out.data)
        r.refresh_from_db()
        kept, back = r.items.order_by("id")
        self.assertEqual(kept.sold_total, D("67"))
        self.assertEqual(back.sold_total, D("33"))
        self.assertEqual(r.total_price, D("100"))
        self.assertEqual(r.refunded_amount, D("33"))

    def test_whole_quantity_returns_the_line(self):
        r = self.sale(4, paid=D("400"))
        line = r.items.get()
        out = self.refund(r, [{"id": line.id, "quantity": "4"}])
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(r.items.count(), 1)
        self.assertTrue(r.items.get().is_returned)

    def test_old_quantity_field_is_still_a_400(self):
        r = self.sale(10, paid=D("1000"))
        out = self.post(r, "refund", {"item_ids": [r.items.get().id], "quantity": 3})
        self.assertEqual(out.status_code, 400)
        self.assertIn("quantities", str(out.data))

    def test_work_with_dimensions_is_rejected(self):
        cut = PrintingService.objects.create(name="Рез", kind=PrintingService.Kind.CUTTING,
                                             rate_flat=D("100"))
        r = self.sale(1, paid=D("100"))
        work = TransactionItem.objects.create(
            receipt=r, type="SERVICE", service=cut, quantity=D("5"), price_per_item=D("100"),
            width=D("1"), length=D("1"),
        )
        r.recalculate_total()
        r.save(update_fields=["total_price"])
        out = self.refund(r, [{"id": work.id, "quantity": "2"}])
        self.assertEqual(out.status_code, 400)
        self.assertEqual(r.items.count(), 2)


class PartialRefundFromLotsTests(CashOpsBase):
    def test_each_lot_gets_its_share_back(self):
        mat = Material.objects.create(
            name="Диод", unit=Material.Unit.PIECE, price_per_unit=D("100"), piece_price=D("100"),
        )
        now = timezone.now()
        a = receive_lot(mat, form=Roll.Form.PIECE, purchase_cost=D("240"), sheet_count=6,
                        received_at=now - timedelta(days=2), user=self.admin)      # 40 за шт
        b = receive_lot(mat, form=Roll.Form.PIECE, purchase_cost=D("240"), sheet_count=4,
                        received_at=now - timedelta(days=1), user=self.admin)      # 60 за шт
        r = create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": mat, "quantity": 10, "mode": "PIECE"}],
            pay_full=True,
        )
        line = r.items.get()
        self.assertEqual(line.cost_total, D("480"))
        refund_receipt(r, user=self.admin, quantities={line.id: "5"})
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.remaining_area, b.remaining_area), (D("3"), D("2")))  # по половине
        kept, back = r.items.order_by("id")
        self.assertEqual((kept.cost_total, back.cost_total), (D("240"), D("240")))
        mat.refresh_from_db()
        self.assertEqual(mat.quantity, D("5"))
