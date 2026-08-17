"""Зачёт сдачи: невыданная сдача клиента гасит его следующий заказ.

Заказ на 9 000, клиент принёс 10 000, мелочи в кассе не нашлось — 1 000 висит
сдачей. Приходит он же за новым заказом, и до сих пор эта тысяча в оплату не шла
никак: её надо было сперва выдать на руки, а потом принять обратно.

Денег зачёт НЕ двигает — они лежат в кассе с прошлого раза, — поэтому в кассовой
книге его нет, а в чеке он виден отдельным полем `change_applied`.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Receipt
from sales.sale_service import (
    client_change_available,
    create_sale,
    delete_receipt,
    give_change,
)
from warehouse.models import Material


class ChangeAppliedTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss_ca", password="x", role=User.Role.ADMIN
        )
        self.client_obj = Client.objects.create(full_name="Тахир", phone="+996555111333")
        self.other = Client.objects.create(full_name="Азамат", phone="+996555111444")
        self.material = Material.objects.create(
            name="Крепёж",
            unit=Material.Unit.PIECE,
            quantity=Decimal("10000"),
            price_per_unit=Decimal("100"),
            piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )

    def _sale(self, *, qty, paid=None, client=None, use_change=False, pay_full=False):
        """Заказ на qty × 100 сом."""
        return create_sale(
            client=self.client_obj if client is None else client,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.material, "quantity": qty}],
            amount_paid=paid,
            pay_full=pay_full,
            use_change=use_change,
        )

    def _cash_in(self):
        """Сколько всего денег легло в кассу по продажам."""
        return sum(
            (e.amount for e in CashEntry.objects.filter(article=CashEntry.Article.SALE)),
            Decimal("0"),
        )

    def test_change_closes_the_next_order(self):
        first = self._sale(qty=90, paid=Decimal("10000"))   # 9000, принесли 10000
        self.assertEqual(first.change_due, Decimal("1000"))

        # Новый заказ на 3000: клиент доплачивает 2000, тысяча идёт зачётом.
        second = self._sale(qty=30, paid=Decimal("2000"), use_change=True)
        self.assertEqual(second.total_price, Decimal("3000"))
        self.assertEqual(second.change_applied, Decimal("1000"))
        self.assertEqual(second.amount_paid, Decimal("3000"))
        self.assertEqual(second.debt, Decimal("0"))
        self.assertEqual(second.payment_status, Receipt.PaymentStatus.PAID)

        first.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("0"))
        self.assertEqual(client_change_available(self.client_obj), Decimal("0"))

        # В кассу пришли только настоящие деньги: 10 000 + 2 000.
        self.assertEqual(self._cash_in(), Decimal("12000"))

    def test_change_is_only_used_for_what_is_left_unpaid(self):
        """Клиент внёс деньгами всю сумму — сдачу трогать не за что."""
        first = self._sale(qty=90, paid=Decimal("10000"))
        second = self._sale(qty=30, paid=Decimal("3000"), use_change=True)
        self.assertEqual(second.change_applied, Decimal("0"))
        first.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("1000"))

    def test_pay_full_with_offset_takes_only_the_rest_in_cash(self):
        """«Вся сумма» при включённом зачёте — это остаток после сдачи.

        Заказ 3 000, у клиента 1 000 сдачи: в кассу кладут 2 000, заказ закрыт.
        """
        first = self._sale(qty=90, paid=Decimal("10000"))
        second = self._sale(qty=30, pay_full=True, use_change=True)
        self.assertEqual(second.change_applied, Decimal("1000"))
        self.assertEqual(second.amount_paid, Decimal("3000"))
        self.assertEqual(second.change_due, Decimal("0"))
        self.assertEqual(second.payment_status, Receipt.PaymentStatus.PAID)
        first.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("0"))
        # Деньгами приняли только доплату: 10 000 + 2 000.
        self.assertEqual(self._cash_in(), Decimal("12000"))

    def test_pay_full_when_change_covers_everything(self):
        """Сдачи хватает на весь заказ — деньги в кассу не идут вовсе."""
        first = self._sale(qty=10, paid=Decimal("5000"))     # 1000, сдачи 4000
        second = self._sale(qty=10, pay_full=True, use_change=True)
        self.assertEqual(second.change_applied, Decimal("1000"))
        self.assertEqual(second.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(second.change_due, Decimal("0"))
        first.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("3000"))
        self.assertEqual(self._cash_in(), Decimal("5000"))

    def test_change_is_not_used_without_the_flag(self):
        """Галочка снята — сдача остаётся лежать, заказ уходит в долг."""
        first = self._sale(qty=90, paid=Decimal("10000"))
        second = self._sale(qty=30)
        self.assertEqual(second.change_applied, Decimal("0"))
        self.assertEqual(second.debt, Decimal("3000"))
        first.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("1000"))

    def test_only_this_client_change_is_used(self):
        """Сдача чужого клиента чужой заказ не гасит."""
        self._sale(qty=90, paid=Decimal("10000"), client=self.other)
        mine = self._sale(qty=30, use_change=True)
        self.assertEqual(mine.change_applied, Decimal("0"))
        self.assertEqual(mine.debt, Decimal("3000"))

    def test_change_bigger_than_the_order_is_taken_partially(self):
        first = self._sale(qty=10, paid=Decimal("5000"))     # 1000, сдачи 4000
        second = self._sale(qty=10, use_change=True)         # 1000 целиком зачётом
        self.assertEqual(second.change_applied, Decimal("1000"))
        self.assertEqual(second.payment_status, Receipt.PaymentStatus.PAID)
        first.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("3000"))
        # Касса не изменилась: второй заказ денег не принёс.
        self.assertEqual(self._cash_in(), Decimal("5000"))

    def test_change_is_taken_from_the_oldest_order_first(self):
        old = self._sale(qty=10, paid=Decimal("1600"))       # сдачи 600
        recent = self._sale(qty=10, paid=Decimal("1500"))    # сдачи 500
        third = self._sale(qty=10, use_change=True)          # нужно 1000
        self.assertEqual(third.change_applied, Decimal("1000"))
        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(old.change_due, Decimal("0"))       # старый выбран целиком
        self.assertEqual(recent.change_due, Decimal("100"))  # с нового добрали 400

    def test_partial_change_leaves_the_rest_as_debt(self):
        self._sale(qty=10, paid=Decimal("1500"))             # сдачи 500
        second = self._sale(qty=30, use_change=True)         # заказ 3000
        self.assertEqual(second.change_applied, Decimal("500"))
        self.assertEqual(second.amount_paid, Decimal("500"))
        self.assertEqual(second.debt, Decimal("2500"))
        self.assertEqual(second.payment_status, Receipt.PaymentStatus.PENDING)

    def test_deleting_the_order_gives_the_change_back(self):
        """Заказа не было — значит и тратить сдачу было не на что."""
        first = self._sale(qty=90, paid=Decimal("10000"))
        second = self._sale(qty=30, paid=Decimal("2000"), use_change=True)
        delete_receipt(second, user=self.admin)
        self.assertEqual(client_change_available(self.client_obj), Decimal("1000"))
        first.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("1000"))

    def test_change_given_out_cannot_be_used_again(self):
        first = self._sale(qty=90, paid=Decimal("10000"))
        give_change(first, user=self.admin)                  # отдали на руки
        second = self._sale(qty=30, use_change=True)
        self.assertEqual(second.change_applied, Decimal("0"))
        self.assertEqual(second.debt, Decimal("3000"))

    def test_walk_in_client_has_nothing_to_offset(self):
        """Заказ без клиента: чужую сдачу зачесть нельзя, падать тоже нельзя."""
        self._sale(qty=90, paid=Decimal("10000"))
        anon = create_sale(
            client=None,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.material, "quantity": 10}],
            use_change=True,
        )
        self.assertEqual(anon.change_applied, Decimal("0"))
        self.assertEqual(anon.debt, Decimal("1000"))
