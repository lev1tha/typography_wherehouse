"""«Наличные 449 042» — это заказы, а не деньги в ящике.

Проверка прод-данных 19.09.2026: в «Обзоре» выручка разложена по способам
оплаты, и наличными там стояло 449 042 сома при 114 707 в кассе. Разница —
314 141 долга: способ оплаты стоит в ЗАКАЗЕ, и заказ, отданный в долг, попадал
в свою долю целиком. Рядом с каждым способом теперь стоит, сколько по нему уже
получено и сколько ещё должны.

Способ у полученных денег берётся у самой оплаты, а не у чека: долг часто гасят
не тем способом, которым оформляли заказ. Поэтому «получено наличными» здесь и
наличный остаток кассовой книги — об одном и том же.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material


class RevenueSplitTests(APITestCase):
    URL = "/api/audit/dashboard/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="rs_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.customer = Client.objects.create(full_name="Клиент", phone="+996700000555")
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE,
            quantity=Decimal("1000"), price_per_unit=Decimal("100"),
            purchase_price=Decimal("40"),
        )

    def _sale(self, *, qty, paid, method=Receipt.PaymentMethod.CASH):
        return sale_service.create_sale(
            client=self.customer, cashier=self.admin, payment_method=method,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal(qty), "mode": "SQM",
            }],
            amount_paid=Decimal(paid),
        )

    def _revenue(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["revenue"]

    def test_credit_order_is_revenue_but_not_money(self):
        self._sale(qty="10", paid="0")            # 1000 наличными, в долг
        rev = self._revenue()
        self.assertEqual(Decimal(str(rev["cash"])), Decimal("1000"))
        self.assertEqual(Decimal(str(rev["received"]["cash"])), Decimal("0"))
        self.assertEqual(Decimal(str(rev["debt"]["cash"])), Decimal("1000"))
        # Ровно это и разводило плитку с кассой: денег нет, а выручка есть.
        self.assertEqual(CashEntry.balance(CashEntry.Account.CASH), Decimal("0"))

    def test_received_follows_the_payment_not_the_order(self):
        """Наличный заказ, погашенный переводом, — деньги в банке, не в ящике."""
        receipt = self._sale(qty="10", paid="0")
        sale_service.apply_payment(
            receipt, Decimal("400"), user=self.admin, method=Receipt.PaymentMethod.MBANK
        )
        rev = self._revenue()
        self.assertEqual(Decimal(str(rev["cash"])), Decimal("1000"))      # заказ
        self.assertEqual(Decimal(str(rev["received"]["cash"])), Decimal("0"))
        self.assertEqual(Decimal(str(rev["received"]["mbank"])), Decimal("400"))
        self.assertEqual(Decimal(str(rev["debt"]["cash"])), Decimal("600"))

    def test_received_matches_the_cash_book(self):
        """Главная сверка: «получено» в Обзоре == приход кассовой книги."""
        self._sale(qty="5", paid="500")                                   # нал
        self._sale(qty="3", paid="300", method=Receipt.PaymentMethod.MBANK)
        receipt = self._sale(qty="10", paid="0")                          # в долг
        sale_service.apply_payment(
            receipt, Decimal("250"), user=self.admin, method=Receipt.PaymentMethod.CASH
        )
        rev = self._revenue()
        self.assertEqual(Decimal(str(rev["received"]["cash"])), Decimal("750"))
        self.assertEqual(Decimal(str(rev["received"]["mbank"])), Decimal("300"))
        self.assertEqual(Decimal(str(rev["received"]["total"])), Decimal("1050"))
        self.assertEqual(
            CashEntry.balance(CashEntry.Account.CASH), Decimal("750")
        )
        self.assertEqual(
            CashEntry.balance(CashEntry.Account.BANK), Decimal("300")
        )

    def test_totals_agree_with_the_finance_report(self):
        """«Получено» и «долг» — те же числа, что в «Финансах»."""
        receipt = self._sale(qty="10", paid="0")
        sale_service.apply_payment(receipt, Decimal("400"), user=self.admin)
        self._sale(qty="2", paid="200", method=Receipt.PaymentMethod.DEMIRBANK)
        rev = self._revenue()
        fin = self.client.get("/api/finance/report/").data
        self.assertEqual(
            Decimal(str(rev["received"]["total"])), Decimal(str(fin["revenue_paid"]))
        )
        self.assertEqual(
            Decimal(str(rev["debt"]["total"])), Decimal(str(fin["client_debt"]))
        )
        self.assertEqual(Decimal(str(rev["total"])), Decimal(str(fin["revenue"])))

    def test_received_never_exceeds_what_the_order_is_worth(self):
        """Принесли больше стоимости — лишнее это сдача, а не выручка."""
        self._sale(qty="5", paid="700")           # заказ на 500
        rev = self._revenue()
        self.assertEqual(Decimal(str(rev["received"]["cash"])), Decimal("500"))
        self.assertEqual(Decimal(str(rev["received"]["total"])), Decimal("500"))
