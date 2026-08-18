"""Онлайн-оплата обязана попасть в кассовую книгу.

Приход денег писали только `create_sale` (оплата при оформлении) и
`apply_payment` (погашение долга). Онлайн-заказ шёл мимо обоих: чек становился
«Оплачено», выручка в отчёте росла, а «Касса и банк» этих денег не видела —
свести остаток по счёту было нечем, и расхождение выглядело как ошибка отчёта.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material


class OnlinePaymentCashTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="oc_admin", password="x", role=User.Role.ADMIN
        )
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "ONLINE",
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 5}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.receipt = Receipt.objects.get(pk=r.data["id"])

    def test_unpaid_online_order_is_not_in_the_cash_book(self):
        """Счёт выставлен, денег ещё нет — записи быть не должно."""
        self.assertFalse(CashEntry.objects.filter(receipt=self.receipt).exists())

    def test_confirmed_online_payment_lands_on_the_bank_account(self):
        sale_service.confirm_payment(self.receipt)

        entries = CashEntry.objects.filter(receipt=self.receipt)
        self.assertEqual(entries.count(), 1, "приход по онлайн-оплате не записан")
        entry = entries.get()
        self.assertEqual(entry.kind, CashEntry.Kind.IN)
        self.assertEqual(entry.article, CashEntry.Article.SALE)
        # Онлайн — не наличные: деньги идут на счёт, а не в ящик.
        self.assertEqual(entry.account, CashEntry.Account.BANK)
        self.assertEqual(entry.amount, Decimal("50"))

    def test_balance_grows_by_the_paid_amount(self):
        before = CashEntry.balance(account=CashEntry.Account.BANK)
        sale_service.confirm_payment(self.receipt)
        after = CashEntry.balance(account=CashEntry.Account.BANK)
        self.assertEqual(after - before, Decimal("50"))

    def test_second_confirmation_does_not_double_the_money(self):
        """Шлюз может прислать подтверждение дважды — касса не должна удвоиться."""
        sale_service.confirm_payment(self.receipt)
        sale_service.confirm_payment(self.receipt)
        self.assertEqual(CashEntry.objects.filter(receipt=self.receipt).count(), 1)

    def test_reverting_the_payment_takes_the_money_back_out(self):
        """Откат оплаты пишет встречный расход — остаток возвращается к нулю."""
        sale_service.confirm_payment(self.receipt)
        before = CashEntry.balance(account=CashEntry.Account.BANK)
        resp = self.client.post(f"/api/sales/receipts/{self.receipt.id}/unpay/")
        self.assertEqual(resp.status_code, 200, resp.data)
        after = CashEntry.balance(account=CashEntry.Account.BANK)
        self.assertEqual(before - after, Decimal("50"))
