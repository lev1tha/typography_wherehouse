"""Касса сходится с ящиком: деньги уходят туда, откуда пришли, и не пропадают.

Аудит 26.09:
- п. 4: откат оплаты и возврат писали расход по способу ЧЕКА. Чек №40
  «наличные» оплатили переводом 507, откатили — наличные −507, банк не тронут;
- п. 8: переплата при оплате долга становилась сдачей, но в кассу не
  приходовалась: принесли 3 700 за долг 3 200 — касса +3 200, после выдачи
  сдачи +2 700 при реальных +3 200;
- п. 9: откат оплаты заказа, закрытого сдачей с прошлого раза, уводил кассу
  в −60 и съедал сдачу клиента — он снова был должен 60;
- п. 10: удаление оплаченного заказа стирало приход каскадом — касса −700, и
  в книге ни строки.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Receipt
from sales.sale_service import create_sale
from warehouse.models import Material

CASH, BANK = CashEntry.Account.CASH, CashEntry.Account.BANK


class CashTrailTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="ct_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.customer = Client.objects.create(full_name="Ош Принт", phone="+996555123123")
        self.bolt = Material.objects.create(
            name="Болт", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            purchase_price=Decimal("5"), price_per_unit=Decimal("507"),
        )

    def _sale(self, *, paid="0", qty=1, client=None):
        return create_sale(
            client=client or self.customer, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.bolt,
                         "quantity": Decimal(qty), "mode": "PIECE"}],
            amount_paid=Decimal(paid),
        )

    def _pay(self, receipt, amount, method="CASH"):
        r = self.client.post(f"/api/sales/receipts/{receipt.id}/pay/",
                             {"amount": str(amount), "method": method}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    # --- п. 4 ------------------------------------------------------------
    def test_unpay_takes_the_money_back_from_the_account_it_came_to(self):
        receipt = self._sale()
        self._pay(receipt, "507", method="MBANK")
        r = self.client.post(f"/api/sales/receipts/{receipt.id}/unpay/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))
        self.assertEqual(CashEntry.balance(BANK), Decimal("0"))

    def test_refund_goes_out_of_the_account_the_money_came_to(self):
        receipt = self._sale(qty=2)
        self._pay(receipt, "1014", method="MBANK")
        line = receipt.items.get()
        # Возвращаем весь заказ: 1 014 уходят с банка, наличные не трогаем.
        r = self.client.post(f"/api/sales/receipts/{receipt.id}/refund/",
                             {"item_ids": [line.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(BANK), Decimal("0"))
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    # --- п. 8 ------------------------------------------------------------
    def test_bulk_debt_overpay_puts_the_change_into_the_cash_book(self):
        self._sale(qty=6)  # долг 3 042
        r = self.client.post(f"/api/clients/clients/{self.customer.id}/pay-debt/",
                             {"amount": "3542", "method": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # В ящик легло всё принесённое.
        self.assertEqual(CashEntry.balance(CASH), Decimal("3542"))
        last = Receipt.objects.filter(client=self.customer, change_due__gt=0).get()
        r = self.client.post(f"/api/sales/receipts/{last.id}/give-change/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # Отдали сдачу — в книге ровно долг, как и в ящике.
        self.assertEqual(CashEntry.balance(CASH), Decimal("3042"))

    def test_single_order_overpay_puts_the_change_into_the_cash_book(self):
        receipt = self._sale()
        self._pay(receipt, "1000")
        receipt.refresh_from_db()
        self.assertEqual(receipt.change_due, Decimal("493"))
        self.assertEqual(CashEntry.balance(CASH), Decimal("1000"))

    # --- п. 9 ------------------------------------------------------------
    def test_unpaying_an_order_closed_by_change_does_not_touch_the_cash(self):
        old = self._sale(paid="1507")  # переплата 1 000 — сдача на старом заказе
        self.assertEqual(old.change_due, Decimal("1000"))
        cash_before = CashEntry.balance(CASH)
        new = create_sale(
            client=self.customer, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.bolt,
                         "quantity": Decimal("1"), "mode": "PIECE"}],
            use_change=True,
        )
        self.assertEqual(new.change_applied, Decimal("507"))
        r = self.client.post(f"/api/sales/receipts/{new.id}/unpay/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # Денег по этому заказу не приходило — и уходить нечему.
        self.assertEqual(CashEntry.balance(CASH), cash_before)
        # Сдача клиента вернулась к нему целиком.
        total_change = sum(r.change_due for r in Receipt.objects.filter(client=self.customer))
        self.assertEqual(total_change, Decimal("1000"))
        new.refresh_from_db()
        self.assertEqual(new.change_applied, Decimal("0"))
        self.assertEqual(new.debt, Decimal("507"))

    # --- п. 10 -----------------------------------------------------------
    def test_deleting_a_paid_order_leaves_a_trail_in_the_cash_book(self):
        receipt = self._sale(paid="507")
        number = receipt.order_number
        r = self.client.delete(f"/api/sales/receipts/{receipt.id}/")
        self.assertIn(r.status_code, (200, 204), getattr(r, "data", None))
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))
        rows = list(CashEntry.objects.order_by("id").values_list("kind", "amount", "note"))
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[0][:2], ("IN", Decimal("507.00")))
        self.assertEqual(rows[1][:2], ("OUT", Decimal("507.00")))
        self.assertIn(f"№{number} удалён", rows[1][2])
