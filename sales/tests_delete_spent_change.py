"""Удаление чека, чья сдача уже зачтена в другой заказ.

A 1500, принесли 3000 (сдача 1500 не выдана); сдачу зачли в заказ B на 2100
(оплачено 2000, долг 100). Удалили A — касса −3000, книга 500, у B «оплачено
2000», а реальный долг B 1600. Связь «откуда сдача — куда» нигде не хранится,
поэтому откатывать зачёт автоматически нельзя: удаление отклоняется 400 с
номером заказа-получателя.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Receipt
from sales.sale_service import create_sale
from warehouse.models import Material


class DeleteSpentChangeTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="dsc_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.person = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("1000"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        # A: 15 × 100 = 1500, принесли 3000.
        self.a = self._sale(15, paid="3000")
        # B: 21 × 100 = 2100, принесли 500 и зачли сдачу.
        self.b = self._sale(21, paid="500", use_change=True)

    def _sale(self, qty, *, paid, use_change=False):
        return create_sale(
            client=self.person, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": qty}],
            amount_paid=Decimal(paid), use_change=use_change,
        )

    def _book(self):
        total = Decimal("0")
        for kind, amount in CashEntry.objects.values_list("kind", "amount"):
            total += amount if kind == CashEntry.Kind.IN else -amount
        return total

    def test_scenario_is_as_in_the_audit(self):
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.a.change_due, Decimal("0"))      # сдачу забрали
        self.assertEqual(self.b.change_applied, Decimal("1500"))
        self.assertEqual(self.b.amount_paid, Decimal("2000"))
        self.assertEqual(self._book(), Decimal("3500"))

    def test_deleting_a_is_refused_and_names_b(self):
        resp = self.client.delete(f"/api/sales/receipts/{self.a.id}/")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn(f"№{self.b.order_number}", resp.data["detail"])
        self.assertTrue(Receipt.objects.filter(pk=self.a.pk).exists())
        # Ничего не тронуто: касса и оплата B прежние.
        self.assertEqual(self._book(), Decimal("3500"))
        self.b.refresh_from_db()
        self.assertEqual(self.b.amount_paid, Decimal("2000"))

    def test_after_unpaying_b_the_change_comes_back_and_a_can_be_deleted(self):
        unpay = self.client.post(f"/api/sales/receipts/{self.b.id}/unpay/")
        self.assertEqual(unpay.status_code, 200, unpay.data)
        self.a.refresh_from_db()
        self.assertEqual(self.a.change_due, Decimal("1500"))   # вернулась на A
        resp = self.client.delete(f"/api/sales/receipts/{self.a.id}/")
        self.assertEqual(resp.status_code, 204, resp.data)
        self.assertEqual(self._book(), Decimal("0"))

    def test_plain_deletion_with_unissued_change_still_works(self):
        c = self._sale(15, paid="3000")   # сдача 1500 осталась на C, никуда не зачтена
        resp = self.client.delete(f"/api/sales/receipts/{c.id}/")
        self.assertEqual(resp.status_code, 204, resp.data)

    def test_deleting_the_order_that_took_the_change_gives_it_back(self):
        """B удаляют — сдача возвращается клиенту (прежнее поведение сохранено)."""
        resp = self.client.delete(f"/api/sales/receipts/{self.b.id}/")
        self.assertEqual(resp.status_code, 204, resp.data)
        self.a.refresh_from_db()
        self.assertEqual(self.a.change_due, Decimal("1500"))
