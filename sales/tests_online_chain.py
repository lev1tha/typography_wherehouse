"""Онлайн-цепочка: отмена, возврат строки, дозаказ и подтверждение шлюза.

Неоплаченный онлайн-счёт склад не трогает; списание происходит в
`confirm_payment` (и при оплате в кассе, `/pay/`). Аудит нашёл, где эта цепочка
врёт:
- шлюз подтвердил оплату по ОТМЕНЁННОМУ счёту → чек «Отменён» и «Оплачено»,
  склад списан;
- `_deduct_all` списывал и ВОЗВРАЩЁННЫЕ строки → склад уходил дважды (993 вместо
  996 после «возврат строки + дозаказ до оплаты»);
- `_stock_was_deducted` смотрел на статус оплаты: у онлайн-счёта с возвращённой
  строкой статус «частичный возврат», хотя склад не списан, — и следующий возврат
  или удаление клали на полку то, чего оттуда не брали;
- `_settle` затирал `amount_paid` итогом чека: 400 в кассе + шлюз → в книге 1400
  при заказе на 1000, и ничто не объясняло лишние 400.
"""
from decimal import Decimal

from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from audit.models import AuditLog
from finance.models import CashEntry
from sales.models import Receipt
from sales.sale_service import (
    apply_payment,
    confirm_payment,
    create_sale,
    add_items_to_receipt,
    delete_receipt,
    refund_receipt,
)
from warehouse.models import Material


class OnlineChainTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="chain_boss", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )

    def _online(self, *qtys):
        return create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.ONLINE,
            items_data=[
                {"type": "MATERIAL", "material": self.mat, "quantity": q} for q in qtys
            ],
        )

    def _stock(self):
        self.mat.refresh_from_db()
        return self.mat.quantity

    def _book(self):
        total = Decimal("0")
        for kind, amount in CashEntry.objects.values_list("kind", "amount"):
            total += amount if kind == CashEntry.Kind.IN else -amount
        return total

    # ---- отменённый счёт ------------------------------------------------
    def test_gateway_payment_on_a_cancelled_invoice_does_not_resurrect_it(self):
        r = self._online(3)
        refund_receipt(r, user=self.admin)  # счёт не оплачен → вернули целиком
        r.refresh_from_db()
        self.assertEqual(r.status, Receipt.Status.CANCELLED)

        confirm_payment(Receipt.objects.get(pk=r.pk))

        r.refresh_from_db()
        self.assertEqual(r.status, Receipt.Status.CANCELLED)
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.REFUNDED)
        self.assertFalse(r.stock_deducted)
        self.assertEqual(self._stock(), Decimal("100"))
        self.assertIsNone(r.revenue_recognized_at)
        # Деньги шлюз взял, а заказа нет — это видно в журнале действий, чтобы
        # владелец вернул их клиенту руками.
        self.assertTrue(
            AuditLog.objects.filter(action__contains="отменённому").exists()
        )

    # ---- возвращённые строки не списываются ------------------------------
    def test_returned_line_is_not_deducted_on_confirmation(self):
        r = self._online(4, 3)
        first = r.items.order_by("id").first()
        refund_receipt(Receipt.objects.get(pk=r.pk), item_ids=[first.id], user=self.admin)
        self.assertEqual(self._stock(), Decimal("100"))  # склад не трогали

        confirm_payment(Receipt.objects.get(pk=r.pk))

        self.assertEqual(self._stock(), Decimal("97"))  # только вторая строка

    def test_return_a_line_then_add_items_then_pay(self):
        r = self._online(4, 3)
        first = r.items.order_by("id").first()
        refund_receipt(Receipt.objects.get(pk=r.pk), item_ids=[first.id], user=self.admin)
        add_items_to_receipt(Receipt.objects.get(pk=r.pk), [
            {"type": "MATERIAL", "material": self.mat, "quantity": 3},
        ], user=self.admin)
        # До оплаты склад не тронут — ни дозаказом, ни возвратом.
        self.assertEqual(self._stock(), Decimal("100"))

        confirm_payment(Receipt.objects.get(pk=r.pk))

        self.assertEqual(self._stock(), Decimal("94"))  # 3 + 3, а не 4 + 3 + 3

    # ---- _stock_was_deducted смотрит на флаг -----------------------------
    def test_refunding_another_line_of_an_unpaid_invoice_does_not_inflate_stock(self):
        r = self._online(4, 3, 2)
        a, b, _c = r.items.order_by("id")
        refund_receipt(Receipt.objects.get(pk=r.pk), item_ids=[a.id], user=self.admin)
        refund_receipt(Receipt.objects.get(pk=r.pk), item_ids=[b.id], user=self.admin)
        self.assertEqual(self._stock(), Decimal("100"))  # не пополнили из воздуха

    def test_deleting_an_invoice_with_a_returned_line_does_not_inflate_stock(self):
        r = self._online(4, 3)
        a = r.items.order_by("id").first()
        refund_receipt(Receipt.objects.get(pk=r.pk), item_ids=[a.id], user=self.admin)
        delete_receipt(Receipt.objects.get(pk=r.pk), user=self.admin)
        self.assertEqual(self._stock(), Decimal("100"))

    def test_cash_receipt_without_the_flag_still_counts_as_deducted(self):
        """Старый наличный чек без флага `stock_deducted` — склад на нём уходил
        всегда; подстраховка для него остаётся."""
        r = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": 5}],
            pay_full=True,
        )
        Receipt.objects.filter(pk=r.pk).update(stock_deducted=False)
        refund_receipt(Receipt.objects.get(pk=r.pk), user=self.admin)
        self.assertEqual(self._stock(), Decimal("100"))

    # ---- деньги ----------------------------------------------------------
    def test_gateway_after_part_payment_in_the_till_keeps_the_books_whole(self):
        r = self._online(10)  # 1000
        apply_payment(Receipt.objects.get(pk=r.pk), Decimal("400"), user=self.admin)
        self.assertEqual(self._book(), Decimal("400"))

        confirm_payment(Receipt.objects.get(pk=r.pk))

        r.refresh_from_db()
        self.assertEqual(self._book(), Decimal("1400"))  # оба платежа реально пришли
        self.assertEqual(r.amount_paid, Decimal("1000"))
        # Лишние 400 — переплата: деньги в кассе, цех должен их клиенту.
        self.assertEqual(r.change_due, Decimal("400"))
        self.assertEqual(r.amount_paid + r.change_due, self._book())
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(self._stock(), Decimal("90"))

    def test_plain_gateway_payment_is_unchanged(self):
        r = self._online(5)
        confirm_payment(Receipt.objects.get(pk=r.pk))
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("500"))
        self.assertEqual(r.change_due, Decimal("0"))
        self.assertEqual(self._book(), Decimal("500"))

    def test_gateway_on_a_partly_refunded_invoice_keeps_the_refund_out_of_the_total(self):
        r = self._online(4, 3)
        a = r.items.order_by("id").first()
        refund_receipt(Receipt.objects.get(pk=r.pk), item_ids=[a.id], user=self.admin)
        confirm_payment(Receipt.objects.get(pk=r.pk))
        r.refresh_from_db()
        # Клиент должен за оставшуюся строку: 3 × 100.
        self.assertEqual(r.amount_paid, Decimal("300"))
        self.assertEqual(r.debt, Decimal("0"))
        self.assertEqual(r.amount_paid + r.change_due, self._book())


class PayMethodTests(APITestCase):
    """`/pay/`: способ оплаты проверяется, а не уходит в банк по умолчанию."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="paym_boss", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        self.r = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.MBANK,
            items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": 5}],
            amount_paid=None,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def _pay(self, method):
        return self.api.post(
            f"/api/sales/receipts/{self.r.id}/pay/", {"amount": "100", "method": method},
            format="json",
        )

    def test_lowercase_method_is_normalised_to_cash(self):
        resp = self._pay("cash")
        self.assertEqual(resp.status_code, 200, resp.data)
        entry = CashEntry.objects.get(receipt=self.r)
        self.assertEqual(entry.account, CashEntry.Account.CASH)
        self.assertEqual(self.r.payments.get().method, "CASH")

    def test_unknown_method_is_refused(self):
        resp = self._pay("bitcoin")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(CashEntry.objects.filter(receipt=self.r).exists())
        self.r.refresh_from_db()
        self.assertEqual(self.r.amount_paid, Decimal("0"))
        self.assertFalse(self.r.payments.exists())

    def test_no_method_uses_the_receipts_own(self):
        resp = self.api.post(
            f"/api/sales/receipts/{self.r.id}/pay/", {"amount": "100"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.r.payments.get().method, "MBANK")
