"""Гонки по одному чеку: второй запрос обязан увидеть результат первого.

У `Receipt` не было `select_for_update`: вьюха читала чек, служба считала по
этому «снимку» и писала поверх — два одновременных запроса на один чек оба
видели «долг есть / сдача есть / не возвращено» и оба проводили операцию. Аудит
воспроизвёл на Postgres: 8 параллельных `/refund/` — 8 записей REFUND в кассе,
`/pay/` ×8 по 100 — 8 оплат при `amount_paid` 100, `/give-change/` ×8 — касса
4000 вместо 500, `confirm_payment` дважды — склад 96 вместо 98.

Здесь два слоя:
- ПОСЛЕДОВАТЕЛЬНЫЕ тесты со «старым объектом»: второй вызов получает чек,
  прочитанный ДО первой операции, — это и есть гонка, только без потоков.
  Идут на любой базе.
- ДВУХПОТОЧНЫЕ тесты (`TransactionTestCase`) — только на PostgreSQL: на SQLite
  `select_for_update` ничего не делает, и «гонку» там не воспроизвести.
"""
import threading
from decimal import Decimal

from django.db import connections
from django.http import Http404
from django.test import TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Payment, Receipt
from sales.sale_service import (
    ItemEditRejected,
    PaymentRejected,
    add_items_to_receipt,
    apply_payment,
    confirm_payment,
    create_sale,
    delete_receipt,
    give_change,
    pay_client_debt,
    refund_receipt,
    update_receipt_items,
)
from warehouse.models import Material


class _Base:
    def make_world(self):
        self.admin = User.objects.create_user(
            username="boss_race", password="x", role=User.Role.ADMIN
        )
        self.person = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )

    def sale(self, *, qty=1, paid=None, method=Receipt.PaymentMethod.CASH, client=True,
             lines=None):
        items = lines or [{"type": "MATERIAL", "material": self.material, "quantity": qty}]
        return create_sale(
            client=self.person if client else None, cashier=self.admin,
            payment_method=method, items_data=items, amount_paid=paid,
        )

    def cash_balance(self):
        total = Decimal("0")
        for kind, amount in CashEntry.objects.values_list("kind", "amount"):
            total += amount if kind == CashEntry.Kind.IN else -amount
        return total


class StaleReceiptTests(_Base, APITestCase):
    """Второй вызов получает чек, прочитанный до первого."""

    def setUp(self):
        self.make_world()

    def test_partial_refunds_do_not_lose_each_other(self):
        r = self.sale(lines=[
            {"type": "MATERIAL", "material": self.material, "quantity": 1},
            {"type": "MATERIAL", "material": self.material, "quantity": 2},
        ], paid=Decimal("300"))
        first, second = r.items.order_by("id")
        a, b = Receipt.objects.get(pk=r.pk), Receipt.objects.get(pk=r.pk)
        refund_receipt(a, item_ids=[first.id], user=self.admin)
        refund_receipt(b, item_ids=[second.id], user=self.admin)
        r.refresh_from_db()
        self.assertEqual(r.refunded_amount, Decimal("300"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.REFUNDED)
        # Два возврата — две записи (100 и 200), но каждая ровно один раз.
        self.assertEqual(CashEntry.objects.filter(article="REFUND").count(), 2)
        self.assertEqual(self.cash_balance(), Decimal("0"))

    def test_second_refund_of_same_item_is_rejected(self):
        r = self.sale(qty=2, paid=Decimal("200"))
        a, b = Receipt.objects.get(pk=r.pk), Receipt.objects.get(pk=r.pk)
        refund_receipt(a, user=self.admin)
        with self.assertRaises(ItemEditRejected):
            refund_receipt(b, user=self.admin)
        self.assertEqual(CashEntry.objects.filter(article="REFUND").count(), 1)
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("100"))

    def test_second_payment_is_rejected_not_doubled(self):
        r = self.sale(qty=1, paid=None)  # долг 100
        a, b = Receipt.objects.get(pk=r.pk), Receipt.objects.get(pk=r.pk)
        apply_payment(a, Decimal("100"), user=self.admin, keep_change=True)
        with self.assertRaises(PaymentRejected):
            apply_payment(b, Decimal("100"), user=self.admin, keep_change=True)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("100"))
        self.assertEqual(Payment.objects.filter(receipt=r).count(), 1)
        self.assertEqual(self.cash_balance(), Decimal("100"))

    def test_partial_payments_add_up_instead_of_overwriting(self):
        r = self.sale(qty=2, paid=None)  # долг 200
        a, b = Receipt.objects.get(pk=r.pk), Receipt.objects.get(pk=r.pk)
        apply_payment(a, Decimal("50"), user=self.admin)
        apply_payment(b, Decimal("50"), user=self.admin)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("100"))
        self.assertEqual(self.cash_balance(), Decimal("100"))

    def test_second_confirm_payment_does_not_deduct_stock_twice(self):
        r = self.sale(qty=2, method=Receipt.PaymentMethod.ONLINE, client=False)
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("100"))  # не списан
        a, b = Receipt.objects.get(pk=r.pk), Receipt.objects.get(pk=r.pk)
        confirm_payment(a)
        confirm_payment(b)
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("98"))
        self.assertEqual(CashEntry.objects.filter(receipt=r).count(), 1)
        self.assertEqual(self.cash_balance(), Decimal("200"))

    def test_second_give_change_is_limited_by_what_is_left(self):
        r = self.sale(qty=1, paid=Decimal("600"))  # сдача 500
        self.assertEqual(r.change_due, Decimal("500"))
        a, b = Receipt.objects.get(pk=r.pk), Receipt.objects.get(pk=r.pk)
        self.assertEqual(give_change(a, None, user=self.admin), Decimal("500"))
        with self.assertRaises(PaymentRejected):
            give_change(b, None, user=self.admin)
        self.assertEqual(self.cash_balance(), Decimal("100"))

    def test_add_items_keeps_a_payment_taken_meanwhile(self):
        r = self.sale(qty=2, paid=None)
        stale = Receipt.objects.get(pk=r.pk)
        apply_payment(Receipt.objects.get(pk=r.pk), Decimal("50"), user=self.admin)
        add_items_to_receipt(stale, [
            {"type": "MATERIAL", "material": self.material, "quantity": 1},
        ], user=self.admin)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("50"))
        self.assertEqual(r.total_price, Decimal("300"))

    def test_edit_items_keeps_change_created_meanwhile(self):
        r = self.sale(qty=10, paid=Decimal("1000"))
        item = r.items.get()
        stale = Receipt.objects.get(pk=r.pk)
        update_receipt_items(
            Receipt.objects.get(pk=r.pk), [{"id": item.id, "quantity": "5"}], user=self.admin
        )
        r.refresh_from_db()
        self.assertEqual(r.change_due, Decimal("500"))
        update_receipt_items(stale, [{"id": item.id, "quantity": "8"}], user=self.admin)
        r.refresh_from_db()
        # Было 1000 оплачено; после 5 шт — 500 сдача; после 8 шт итог 800,
        # оплачено 500, сдача 500 остаётся (деньги в кассе те же).
        self.assertEqual(r.change_due, Decimal("500"))
        self.assertEqual(r.amount_paid, Decimal("500"))
        self.assertEqual(r.debt, Decimal("300"))

    def test_deleting_twice_is_not_found_not_a_crash(self):
        r = self.sale(qty=1, paid=Decimal("100"))
        a, b = Receipt.objects.get(pk=r.pk), Receipt.objects.get(pk=r.pk)
        delete_receipt(a, user=self.admin)
        with self.assertRaises(Http404):
            delete_receipt(b, user=self.admin)
        self.assertEqual(self.cash_balance(), Decimal("0"))

    def test_unpay_twice_writes_one_reverse_entry(self):
        r = self.sale(qty=1, paid=Decimal("100"))
        api = APIClient()
        api.force_authenticate(self.admin)
        first = api.post(f"/api/sales/receipts/{r.id}/unpay/")
        second = api.post(f"/api/sales/receipts/{r.id}/unpay/")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(self.cash_balance(), Decimal("0"))


@skipUnlessDBFeature("has_select_for_update")
class ParallelReceiptTests(_Base, TransactionTestCase):
    """Настоящие потоки — на PostgreSQL. На SQLite этот класс пропускается."""

    def setUp(self):
        self.make_world()

    def _parallel(self, n, fn):
        """Запускает `fn(i)` в n потоках одновременно. Возвращает список
        результатов; исключение потока кладётся в список как значение."""
        barrier = threading.Barrier(n)
        out = [None] * n

        def worker(i):
            try:
                barrier.wait(timeout=10)
                out[i] = fn(i)
            except Exception as exc:  # noqa: BLE001 - результат читает тест
                out[i] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        return out

    def _api(self):
        api = APIClient()
        api.force_authenticate(User.objects.get(username="boss_race"))
        return api

    def test_parallel_refunds_make_one_cash_entry(self):
        r = self.sale(qty=2, paid=Decimal("200"))
        codes = self._parallel(
            6, lambda i: self._api().post(f"/api/sales/receipts/{r.id}/refund/", {}).status_code
        )
        self.assertEqual(sorted(codes).count(200), 1, codes)
        self.assertEqual(CashEntry.objects.filter(article="REFUND").count(), 1)
        self.assertEqual(self.cash_balance(), Decimal("0"))

    def test_parallel_payments_take_the_debt_once(self):
        r = self.sale(qty=1, paid=None)
        codes = self._parallel(
            6,
            lambda i: self._api().post(
                f"/api/sales/receipts/{r.id}/pay/", {"amount": "100"}
            ).status_code,
        )
        self.assertEqual(sorted(codes).count(200), 1, codes)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("100"))
        self.assertEqual(Payment.objects.filter(receipt=r).count(), 1)
        self.assertEqual(self.cash_balance(), Decimal("100"))

    def test_parallel_give_change_pays_out_once(self):
        r = self.sale(qty=1, paid=Decimal("600"))
        codes = self._parallel(
            6, lambda i: self._api().post(f"/api/sales/receipts/{r.id}/give-change/", {}).status_code
        )
        self.assertEqual(sorted(codes).count(200), 1, codes)
        self.assertEqual(self.cash_balance(), Decimal("100"))

    def test_parallel_confirm_payment_deducts_stock_once(self):
        r = self.sale(qty=2, method=Receipt.PaymentMethod.ONLINE, client=False)

        def run(i):
            confirm_payment(Receipt.objects.get(pk=r.pk))
            return "ok"

        self._parallel(6, run)
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("98"))
        self.assertEqual(self.cash_balance(), Decimal("200"))

    def test_parallel_checkouts_get_distinct_order_numbers(self):
        """Номер чека — Max+1: параллельные оформления не должны падать 500."""
        payload = {
            "payment_method": "CASH",
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 1}],
            "pay_full": True,
        }
        codes = self._parallel(
            4, lambda i: self._api().post("/api/sales/receipts/checkout/", payload, format="json").status_code
        )
        self.assertEqual(codes, [201, 201, 201, 201])
        numbers = list(Receipt.objects.values_list("order_number", flat=True))
        self.assertEqual(len(set(numbers)), 4)

    def test_parallel_bulk_debt_payments_pay_once(self):
        self.sale(qty=1, paid=None)
        self.sale(qty=1, paid=None)

        def run(i):
            try:
                pay_client_debt(self.person, Decimal("200"), user=self.admin)
                return "ok"
            except PaymentRejected:
                return "rejected"

        res = self._parallel(4, run)
        self.assertEqual(res.count("ok"), 1, res)
        self.assertEqual(self.cash_balance(), Decimal("200"))
        self.assertEqual(
            sum(Receipt.objects.values_list("amount_paid", flat=True)), Decimal("200")
        )
