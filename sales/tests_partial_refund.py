"""Возврат ОТДЕЛЬНЫХ позиций — деньги и долг сходятся.

Интерфейс теперь даёт вернуть не весь чек, а отмеченные строки. Два места,
где это раньше расходилось:

- долг считался только у статуса PENDING; после возврата одной строки чек
  становился PARTIALLY_REFUNDED, и остаток долга пропадал из карточки, плиток
  и списка (оплату при этом принять было можно);
- деньги по возврату отдавались как `min(возврат, оплачено)`; при частичной
  оплате и двух возвратах подряд касса отдавала больше, чем принимала.

Теперь клиенту отдают ровно переплату относительно того, что у него ОСТАЁТСЯ:
`max(оплачено − (сумма − возвращено), 0)` до и после возврата, разница — в
кассу расходом.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Receipt
from sales.sale_service import apply_payment, create_sale, refund_receipt
from warehouse.models import Material


class PartialRefundTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="pr_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.client_obj = Client.objects.create(full_name="Гульнара", phone="+996700333444")
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("1000"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"), purchase_price=Decimal("40"),
        )
        self.glue = Material.objects.create(
            name="Клей", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("200"), piece_price=Decimal("200"), purchase_price=Decimal("90"),
        )

    def _sale(self, paid):
        """Заказ 300 + 200 = 500."""
        return create_sale(
            client=self.client_obj, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.bolts, "quantity": 3, "mode": "PIECE"},
                {"type": "MATERIAL", "material": self.glue, "quantity": 1, "mode": "PIECE"},
            ],
            amount_paid=paid,
        )

    def _refunds_out(self, receipt):
        return sum(
            (e.amount for e in CashEntry.objects.filter(receipt=receipt, article=CashEntry.Article.REFUND)),
            Decimal("0"),
        )

    def test_unpaid_order_keeps_its_debt_after_returning_one_line(self):
        r = self._sale(paid=None)  # весь заказ в долг
        glue_line = r.items.get(material=self.glue)
        refund_receipt(r, item_ids=[glue_line.id], user=self.admin)
        r.refresh_from_db()
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(r.refunded_amount, Decimal("200"))
        # За крепёж на 300 он по-прежнему должен — и это видно везде.
        self.assertEqual(r.debt, Decimal("300"))
        rows = self.client.get("/api/sales/receipts/").data["results"]
        self.assertEqual(Decimal(str(next(x for x in rows if x["id"] == str(r.id))["debt"])), Decimal("300"))
        self.assertEqual(Decimal(str(self.client.get("/api/sales/receipts/stats/").data["debt"])), Decimal("300"))
        card = self.client.get(f"/api/clients/clients/{self.client_obj.id}/").data
        self.assertEqual(Decimal(str(card["debt"])), Decimal("300"))
        # Денег не брали — и не возвращаем.
        self.assertEqual(self._refunds_out(r), Decimal("0"))
        # Клей вернулся на полку.
        self.glue.refresh_from_db()
        self.assertEqual(self.glue.quantity, Decimal("100"))

    def test_fully_paid_order_returns_the_line_value_in_cash(self):
        r = self._sale(paid=Decimal("500"))
        glue_line = r.items.get(material=self.glue)
        refund_receipt(r, item_ids=[glue_line.id], user=self.admin)
        r.refresh_from_db()
        self.assertEqual(self._refunds_out(r), Decimal("200"))
        self.assertEqual(r.debt, Decimal("0"))

    def test_partially_paid_order_returns_only_the_overpayment(self):
        """Оплачено 300 из 500. Вернули клей (200): у клиента остаётся крепёж на
        300, оплачено ровно 300 — деньгами не возвращаем ничего, долг 0.
        Потом вернули и крепёж: остаётся 0, а оплачено 300 — отдаём 300."""
        r = self._sale(paid=Decimal("300"))
        glue_line = r.items.get(material=self.glue)
        bolts_line = r.items.get(material=self.bolts)
        refund_receipt(r, item_ids=[glue_line.id], user=self.admin)
        r.refresh_from_db()
        self.assertEqual(self._refunds_out(r), Decimal("0"))
        self.assertEqual(r.debt, Decimal("0"))
        refund_receipt(r, item_ids=[bolts_line.id], user=self.admin)
        r.refresh_from_db()
        self.assertEqual(self._refunds_out(r), Decimal("300"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.REFUNDED)
        self.assertEqual(r.status, Receipt.Status.CANCELLED)

    def test_debt_after_partial_refund_can_be_paid_and_closes(self):
        r = self._sale(paid=None)
        glue_line = r.items.get(material=self.glue)
        refund_receipt(r, item_ids=[glue_line.id], user=self.admin)
        r.refresh_from_db()
        apply_payment(r, user=self.admin)  # закрыть остаток
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("300"))
        self.assertEqual(r.debt, Decimal("0"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)

    def test_api_refund_by_items(self):
        r = self._sale(paid=Decimal("500"))
        glue_line = r.items.get(material=self.glue)
        res = self.client.post(f"/api/sales/receipts/{r.id}/refund/", {"item_ids": [glue_line.id]}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        returned = {i["id"]: i["is_returned"] for i in res.data["items"]}
        self.assertTrue(returned[glue_line.id])
        self.assertFalse(returned[r.items.get(material=self.bolts).id])
        self.assertEqual(Decimal(str(res.data["refunded_amount"])), Decimal("200"))

    def test_client_card_shows_returned_lines_and_counts_like_the_list(self):
        """Карточка клиента после возврата целиком: заказ остаётся в истории с
        зачёркнутыми строками, «Заказов» считает без отменённых — как список."""
        r = self._sale(paid=Decimal("500"))
        refund_receipt(r, user=self.admin)
        card = self.client.get(f"/api/clients/clients/{self.client_obj.id}/").data
        self.assertEqual(card["stats"]["orders_count"], 0)
        self.assertEqual(card["stats"]["cancelled_count"], 1)
        order = next(o for o in card["orders"] if str(o["id"]) == str(r.id))
        self.assertTrue(all(i["is_returned"] for i in order["items"]))
        self.assertEqual(Decimal(str(order["refunded_amount"])), Decimal("500"))
        row = next(x for x in self.client.get("/api/clients/clients/").data["results"] if x["id"] == self.client_obj.id)
        self.assertEqual(row["orders_count"], card["stats"]["orders_count"])
