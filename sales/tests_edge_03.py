from decimal import Decimal
import uuid

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt, TransactionItem
from warehouse.models import Material


class EdgeDebtPayTests(APITestCase):
    def setUp(self):
        self.storekeeper = User.objects.create_user(
            username="store_edge", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_edge", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.storekeeper)
        self.material = Material.objects.create(
            name="Acryl edge", category="Plastic", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("3700"),
        )
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Debtor", phone="+799001"
        )

    def _make_receipt(self, *, total=Decimal("3700"), amount_paid=Decimal("0"),
                      refunded=Decimal("0"), status=Receipt.Status.COMPLETED,
                      payment_status=Receipt.PaymentStatus.PENDING, cashier=None):
        receipt = Receipt.objects.create(
            client=self.customer,
            cashier=cashier or self.storekeeper,
            payment_method=Receipt.PaymentMethod.CASH,
            payment_status=payment_status,
            status=status,
            total_price=total,
            amount_paid=amount_paid,
            refunded_amount=refunded,
            stock_deducted=True,
        )
        TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.MATERIAL,
            material=self.material, quantity=Decimal("1"),
            price_per_item=total, sale_mode=TransactionItem.SaleMode.PIECE,
        )
        return receipt

    def _pay(self, receipt, body):
        return self.client.post(
            "/api/sales/receipts/%s/pay/" % receipt.id, body, format="json"
        )

    def test_pay_zero_amount_400(self):
        receipt = self._make_receipt()
        r = self._pay(receipt, {"amount": 0})
        self.assertEqual(r.status_code, 400, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("0"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_pay_negative_amount_400(self):
        receipt = self._make_receipt()
        r = self._pay(receipt, {"amount": -100})
        self.assertEqual(r.status_code, 400, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("0"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_pay_non_numeric_amount_400(self):
        receipt = self._make_receipt()
        r = self._pay(receipt, {"amount": "abc"})
        self.assertEqual(r.status_code, 400, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("0"))

    def test_pay_special_decimal_nan_is_rejected_not_500(self):
        # SUSPECTED BUG: NaN passes Decimal(str(raw)) without raising, but the
        # comparison amount lessthan-or-equal 0 (outside the try/except, line 193)
        # raises InvalidOperation which is unhandled and yields HTTP 500.
        # Correct behavior is 400. Asserting correct behavior exposes the bug.
        receipt = self._make_receipt()
        r = self._pay(receipt, {"amount": "NaN"})
        self.assertEqual(
            r.status_code, 400,
            "NaN must be rejected as invalid amount (400), not crash with 500. "
            "Got %s." % r.status_code,
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("0"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_pay_infinity_is_rejected_not_500(self):
        receipt = self._make_receipt()
        r = self._pay(receipt, {"amount": "Infinity"})
        self.assertEqual(
            r.status_code, 400,
            "Infinity must be rejected as invalid amount (400). Got %s." % r.status_code,
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("0"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)

    def test_pay_cancelled_receipt_400(self):
        receipt = self._make_receipt(
            status=Receipt.Status.CANCELLED,
            payment_status=Receipt.PaymentStatus.REFUNDED,
        )
        r = self._pay(receipt, {"amount": 100})
        self.assertEqual(r.status_code, 400, r.data)

    def test_pay_already_paid_400(self):
        receipt = self._make_receipt(
            amount_paid=Decimal("3700"),
            payment_status=Receipt.PaymentStatus.PAID,
        )
        r = self._pay(receipt, {"amount": 100})
        self.assertEqual(r.status_code, 400, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("3700"))

    def test_pay_overpay_capped_to_debt(self):
        receipt = self._make_receipt()
        r = self._pay(receipt, {"amount": 10000})
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("3700"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(receipt.debt, Decimal("0"))

    def test_pay_partial_then_full(self):
        receipt = self._make_receipt(amount_paid=Decimal("1000"))
        self.assertEqual(receipt.debt, Decimal("2700"))
        r = self._pay(receipt, {"amount": 700})
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("1700"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(receipt.debt, Decimal("2000"))
        r = self._pay(receipt, {})
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("3700"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(receipt.debt, Decimal("0"))

    def test_pay_empty_amount_closes_full_debt(self):
        receipt = self._make_receipt(amount_paid=Decimal("500"))
        r = self._pay(receipt, {})
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("3700"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(receipt.debt, Decimal("0"))

    def test_pay_empty_string_amount_closes_full_debt(self):
        receipt = self._make_receipt()
        r = self._pay(receipt, {"amount": ""})
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("3700"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)

    def test_pay_target_accounts_for_refunded_amount(self):
        # target equals total minus refunded_amount: 3700 minus 700 is 3000.
        receipt = self._make_receipt(total=Decimal("3700"), refunded=Decimal("700"))
        self.assertEqual(receipt.debt, Decimal("3000"))
        r = self._pay(receipt, {})
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("3000"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(receipt.debt, Decimal("0"))

    def test_pay_nonexistent_receipt_404(self):
        r = self.client.post(
            "/api/sales/receipts/%s/pay/" % uuid.uuid4(),
            {"amount": 100}, format="json",
        )
        self.assertEqual(r.status_code, 404, r.data)

    def test_pay_allowed_for_admin(self):
        receipt = self._make_receipt(cashier=self.storekeeper)
        self.client.force_authenticate(self.admin)
        r = self._pay(receipt, {"amount": 3700})
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
