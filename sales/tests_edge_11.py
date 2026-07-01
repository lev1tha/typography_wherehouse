from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt, TransactionItem
from warehouse.models import Material


class _Base(APITestCase):
    def setUp(self):
        self.storekeeper = User.objects.create_user(
            username="store_u11", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_u11", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.storekeeper)
        self.material = Material.objects.create(
            name="Acryl u11", category="Plastic", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("3700"),
        )
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Debtor11", phone="+711001"
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


class UnpayTests(_Base):
    """POST /receipts/<id>/unpay/ — откат ошибочно принятой оплаты."""

    def _unpay(self, receipt):
        return self.client.post(f"/api/sales/receipts/{receipt.id}/unpay/", {}, format="json")

    def test_unpay_full_paid_returns_to_pending(self):
        r = self._make_receipt(amount_paid=Decimal("3700"), payment_status=Receipt.PaymentStatus.PAID)
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 200, resp.data)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("0"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(r.debt, Decimal("3700"))  # весь долг вернулся

    def test_unpay_partial_pending_resets_to_full_debt(self):
        r = self._make_receipt(amount_paid=Decimal("1000"))
        self.assertEqual(r.debt, Decimal("2700"))
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 200, resp.data)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("0"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(r.debt, Decimal("3700"))

    def test_unpay_legacy_paid_without_amount_paid(self):
        # Старый чек: помечен PAID, но amount_paid=0 (до появления поля). Откат
        # статуса тоже должен работать — вернётся в PENDING, появится долг.
        r = self._make_receipt(amount_paid=Decimal("0"), payment_status=Receipt.PaymentStatus.PAID)
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 200, resp.data)
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(r.debt, Decimal("3700"))

    def test_unpay_without_payment_400(self):
        r = self._make_receipt(amount_paid=Decimal("0"))
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 400, resp.data)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("0"))

    def test_unpay_cancelled_400(self):
        r = self._make_receipt(
            amount_paid=Decimal("3700"),
            payment_status=Receipt.PaymentStatus.REFUNDED,
            status=Receipt.Status.CANCELLED,
        )
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_unpay_refunded_400(self):
        r = self._make_receipt(
            amount_paid=Decimal("3700"),
            payment_status=Receipt.PaymentStatus.REFUNDED,
        )
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_unpay_partially_refunded_400(self):
        r = self._make_receipt(
            amount_paid=Decimal("3700"),
            payment_status=Receipt.PaymentStatus.PARTIALLY_REFUNDED,
        )
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_unpay_then_pay_correctly(self):
        # Сценарий заказчика: оплату приняли по ошибке → откат → принять заново.
        r = self._make_receipt(amount_paid=Decimal("3700"), payment_status=Receipt.PaymentStatus.PAID)
        self.assertEqual(self._unpay(r).status_code, 200)
        r.refresh_from_db()
        self.assertEqual(r.debt, Decimal("3700"))
        # теперь можно снова принять оплату
        pay = self.client.post(f"/api/sales/receipts/{r.id}/pay/", {"amount": 3700}, format="json")
        self.assertEqual(pay.status_code, 200, pay.data)
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(r.debt, Decimal("0"))

    def test_unpay_forbidden_cross_storekeeper(self):
        # Складовщик не видит чужой чек → 404 (get_object в его queryset).
        other = User.objects.create_user(username="other_u11", password="x", role=User.Role.STOREKEEPER)
        r = self._make_receipt(amount_paid=Decimal("3700"), payment_status=Receipt.PaymentStatus.PAID, cashier=other)
        resp = self._unpay(r)
        self.assertEqual(resp.status_code, 404)


class DebtSortTests(_Base):
    """Список чеков: по умолчанию у кого долг выше — вверху, затем по дате;
    сортировку можно менять по клику по колонке (?ordering=...)."""

    def test_receipts_with_debt_come_first(self):
        self.client.force_authenticate(self.admin)
        # вперемешку создаём должников и оплаченных
        self._make_receipt(total=Decimal("100"), amount_paid=Decimal("0"))  # долг 100
        self._make_receipt(total=Decimal("200"), amount_paid=Decimal("200"),
                           payment_status=Receipt.PaymentStatus.PAID)  # долг 0
        self._make_receipt(total=Decimal("300"), amount_paid=Decimal("50"))  # долг 250
        self._make_receipt(total=Decimal("500"), amount_paid=Decimal("0"),
                           status=Receipt.Status.CANCELLED,
                           payment_status=Receipt.PaymentStatus.REFUNDED)  # долг 0 (отменён)

        resp = self.client.get("/api/sales/receipts/")
        self.assertEqual(resp.status_code, 200, resp.data)
        debts = [Decimal(str(row["debt"])) for row in resp.data["results"]]
        has_debt = [1 if d > 0 else 0 for d in debts]
        # Все единицы должны идти ДО всех нулей (флаг не возрастает по списку).
        self.assertEqual(has_debt, sorted(has_debt, reverse=True), debts)
        self.assertEqual(sum(has_debt), 2)  # ровно два должника

    def test_higher_debt_on_top_by_default(self):
        self.client.force_authenticate(self.admin)
        self._make_receipt(total=Decimal("100"))  # долг 100
        self._make_receipt(total=Decimal("300"))  # долг 300
        self._make_receipt(total=Decimal("200"))  # долг 200
        resp = self.client.get("/api/sales/receipts/")
        debts = [Decimal(str(row["debt"])) for row in resp.data["results"]]
        self.assertEqual(debts[:3], [Decimal("300"), Decimal("200"), Decimal("100")])

    def test_equal_debt_newest_first(self):
        self.client.force_authenticate(self.admin)
        older = self._make_receipt(total=Decimal("150"))
        newer = self._make_receipt(total=Decimal("150"))
        now = timezone.now()
        Receipt.objects.filter(pk=older.pk).update(created_at=now - timedelta(hours=1))
        Receipt.objects.filter(pk=newer.pk).update(created_at=now)
        resp = self.client.get("/api/sales/receipts/")
        ids = [str(row["id"]) for row in resp.data["results"]]
        self.assertLess(ids.index(str(newer.id)), ids.index(str(older.id)))

    def test_click_sort_by_total_desc(self):
        self.client.force_authenticate(self.admin)
        self._make_receipt(total=Decimal("100"), amount_paid=Decimal("100"),
                           payment_status=Receipt.PaymentStatus.PAID)
        self._make_receipt(total=Decimal("900"), amount_paid=Decimal("900"),
                           payment_status=Receipt.PaymentStatus.PAID)
        resp = self.client.get("/api/sales/receipts/", {"ordering": "-total_price"})
        self.assertEqual(resp.status_code, 200, resp.data)
        totals = [Decimal(str(row["total_price"])) for row in resp.data["results"]]
        self.assertEqual(totals, sorted(totals, reverse=True))
