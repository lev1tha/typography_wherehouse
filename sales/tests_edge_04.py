"""Edge-case tests for refunds (Возвраты).

Targets sales.sale_service.refund_receipt and POST
/api/sales/receipts/<id>/refund/, plus its interaction with debt / pay.
Where the implementation looks wrong, the test asserts the CORRECT expected
behaviour so it fails and surfaces the bug (marked in comments).
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale
from warehouse.models import Material


class EdgeRefundTests(APITestCase):
    def setUp(self):
        self.store = User.objects.create_user(
            username="store_refund", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_refund", password="x", role=User.Role.ADMIN
        )
        # Admin authenticated so refunds are visible regardless of cashier
        # (storekeepers only see their own receipts).
        self.client.force_authenticate(self.admin)

        # Whole-piece-capable area material (sold by sheet => area deduction).
        self.acrylic = Material.objects.create(
            name="Акрил 3мм", category="Акрил", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("0"),
            price_per_sqm=Decimal("1400"), piece_price=Decimal("3700"),
            piece_area=Decimal("2.00"),
        )
        # Simple piece material for multi-line / partial-refund scenarios.
        self.bolt = Material.objects.create(
            name="Крепёж", category="Фурнитура", unit="PIECE",
            quantity=Decimal("50"), price_per_unit=Decimal("100"),
        )
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Покупатель", phone="+790001"
        )

    # ---- helpers -----------------------------------------------------------
    def _refund(self, receipt_id, item_ids=None):
        body = {} if item_ids is None else {"item_ids": item_ids}
        return self.client.post(
            f"/api/sales/receipts/{receipt_id}/refund/", body, format="json"
        )

    def _paid_cash_receipt(self):
        """Fully-paid CASH receipt: 1 sheet (3700) + 2 bolts (100 each) = 3900."""
        return create_sale(
            client=self.customer,
            cashier=self.store,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"},
                {"type": "MATERIAL", "material": self.bolt, "quantity": 2, "mode": "SQM"},
            ],
        )

    # ---- full refund -------------------------------------------------------
    def test_full_refund_of_paid_cash_receipt(self):
        receipt = self._paid_cash_receipt()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.acrylic.refresh_from_db()
        self.bolt.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("98.00"))  # 100 - 2.00*1
        self.assertEqual(self.bolt.quantity, Decimal("48.00"))     # 50 - 2

        r = self._refund(receipt.id)
        self.assertEqual(r.status_code, 200, r.data)

        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.REFUNDED)
        self.assertEqual(receipt.status, Receipt.Status.CANCELLED)
        self.assertEqual(receipt.refunded_amount, Decimal("3900.00"))
        self.assertEqual(receipt.debt, Decimal("0"))
        self.assertTrue(all(i.is_returned for i in receipt.items.all()))

        self.acrylic.refresh_from_db()
        self.bolt.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("100.00"))
        self.assertEqual(self.bolt.quantity, Decimal("50.00"))

    # ---- partial refund by item -------------------------------------------
    def test_partial_refund_by_item(self):
        receipt = self._paid_cash_receipt()
        bolt_item = receipt.items.get(material=self.bolt)

        r = self._refund(receipt.id, item_ids=[bolt_item.id])
        self.assertEqual(r.status_code, 200, r.data)

        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(receipt.status, Receipt.Status.COMPLETED)
        self.assertEqual(receipt.refunded_amount, Decimal("200.00"))

        bolt_item.refresh_from_db()
        acrylic_item = receipt.items.get(material=self.acrylic)
        self.assertTrue(bolt_item.is_returned)
        self.assertFalse(acrylic_item.is_returned)

        self.acrylic.refresh_from_db()
        self.bolt.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("98.00"))  # unchanged
        self.assertEqual(self.bolt.quantity, Decimal("50.00"))     # restored

    # ---- repeated refund is idempotent ------------------------------------
    def test_repeated_refund_does_not_double_count(self):
        receipt = self._paid_cash_receipt()
        bolt_item = receipt.items.get(material=self.bolt)

        self.assertEqual(self._refund(receipt.id, item_ids=[bolt_item.id]).status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.refunded_amount, Decimal("200.00"))
        self.bolt.refresh_from_db()
        self.assertEqual(self.bolt.quantity, Decimal("50.00"))

        # Refund the SAME (already-returned) line again.
        self.assertEqual(self._refund(receipt.id, item_ids=[bolt_item.id]).status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.refunded_amount, Decimal("200.00"))
        self.bolt.refresh_from_db()
        self.assertEqual(self.bolt.quantity, Decimal("50.00"))

    # ---- online unpaid: stock was never deducted --------------------------
    def test_refund_online_unpaid_does_not_touch_stock_or_money(self):
        receipt = create_sale(
            client=self.customer,
            cashier=self.store,
            payment_method=Receipt.PaymentMethod.ONLINE,
            items_data=[
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"},
            ],
        )
        self.assertFalse(receipt.stock_deducted)
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("100.00"))  # nothing deducted

        r = self._refund(receipt.id)
        self.assertEqual(r.status_code, 200, r.data)

        receipt.refresh_from_db()
        # Stock must NOT be inflated above the real on-hand quantity.
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("100.00"))
        # SUSPECTED BUG: nothing was ever paid, so refunded_amount should be 0;
        # the implementation still adds line_total to refunded_amount.
        self.assertEqual(receipt.refunded_amount, Decimal("0"))

    # ---- stock restoration precision (whole piece) ------------------------
    def test_whole_piece_stock_restored_exactly(self):
        receipt = create_sale(
            client=self.customer,
            cashier=self.store,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 3, "mode": "PIECE"},
            ],
        )
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("94.00"))  # 100 - 2.00*3

        self.assertEqual(self._refund(receipt.id).status_code, 200)
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("100.00"))

    # ---- partial refund must not strand the remaining debt -----------------
    def test_partial_refund_keeps_remaining_debt_payable(self):
        # CASH sale total 3900, prepayment 1000 -> debt 2900, PENDING, stock deducted.
        receipt = create_sale(
            client=self.customer,
            cashier=self.store,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"},
                {"type": "MATERIAL", "material": self.bolt, "quantity": 2, "mode": "SQM"},
            ],
            amount_paid=Decimal("1000"),
        )
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertTrue(receipt.stock_deducted)
        self.assertEqual(receipt.debt, Decimal("2900"))

        bolt_item = receipt.items.get(material=self.bolt)
        self.assertEqual(self._refund(receipt.id, item_ids=[bolt_item.id]).status_code, 200)

        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)

        # SUSPECTED BUG: customer still owes for the kept acrylic line
        # (3700 - 1000 paid - 200 refunded = 2500), but `pay` rejects any
        # non-PENDING receipt, so the remaining debt becomes uncollectable.
        r = self.client.post(
            f"/api/sales/receipts/{receipt.id}/pay/", {"amount": 2500}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("3500"))  # 1000 + 2500

    # ---- full refund forbids further debt payment (correct behaviour) -----
    def test_pay_after_full_refund_is_rejected(self):
        receipt = create_sale(
            client=self.customer,
            cashier=self.store,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.bolt, "quantity": 2, "mode": "SQM"},
            ],
            amount_paid=Decimal("0"),
        )
        self.assertEqual(self._refund(receipt.id).status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, Receipt.Status.CANCELLED)

        r = self.client.post(
            f"/api/sales/receipts/{receipt.id}/pay/", {"amount": 100}, format="json"
        )
        self.assertEqual(r.status_code, 400, r.data)

    # ---- refund with unknown item_ids -------------------------------------
    def test_refund_with_unknown_item_ids_changes_nothing(self):
        receipt = self._paid_cash_receipt()
        r = self._refund(receipt.id, item_ids=[999999])
        self.assertEqual(r.status_code, 200, r.data)

        receipt.refresh_from_db()
        self.assertEqual(receipt.refunded_amount, Decimal("0"))
        self.assertFalse(any(i.is_returned for i in receipt.items.all()))
        self.acrylic.refresh_from_db()
        self.bolt.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("98.00"))
        self.assertEqual(self.bolt.quantity, Decimal("48.00"))
