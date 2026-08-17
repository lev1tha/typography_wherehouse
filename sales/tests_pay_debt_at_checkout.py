"""Долг гасится прямо в кассе, вместе с новой продажей.

Клиент пришёл за новым заказом и заодно отдаёт старый долг. Раньше за этим
надо было бросать наполовину собранный чек и уходить в «Клиенты → Погасить
долг». Теперь это галочка в кассе: заказ оформляется и долг закрывается одной
операцией.

Список заказов под погашение собирается ДО продажи — иначе под него попал бы и
сам новый заказ, и с клиента взяли бы больше, чем он должен.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Payment, Receipt
from warehouse.models import Material


class PayDebtAtCheckoutTests(APITestCase):
    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss_pd", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="skl_pd", password="x", role=User.Role.STOREKEEPER
        )
        self.client_obj = Client.objects.create(
            full_name="Тахир", phone="+996555222111"
        )
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("10000"),
            price_per_unit=Decimal("100"), piece_price=Decimal("100"),
            purchase_price=Decimal("40"),
        )
        self.client.force_authenticate(self.admin)

    def _sale(self, *, qty, paid=None, pay_debt=False, pay_full=False):
        body = {
            "payment_method": "CASH",
            "client_id": self.client_obj.id,
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": qty}],
        }
        if paid is not None:
            body["amount_paid"] = str(paid)
        if pay_full:
            body["pay_full"] = True
        if pay_debt:
            body["pay_debt"] = True
        return self.client.post(self.URL, body, format="json")

    def _cash_in(self):
        return sum(
            (e.amount for e in CashEntry.objects.filter(article=CashEntry.Article.SALE)),
            Decimal("0"),
        )

    def test_old_debt_is_closed_together_with_the_new_sale(self):
        first = self._sale(qty=10)                      # 1000 в долг
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(Decimal(first.data["debt"]), Decimal("1000"))

        second = self._sale(qty=5, pay_full=True, pay_debt=True)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(Decimal(str(second.data["debt_paid"])), Decimal("1000"))

        old = Receipt.objects.get(id=first.data["id"])
        new = Receipt.objects.get(id=second.data["id"])
        self.assertEqual(old.debt, Decimal("0"))
        self.assertEqual(old.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(new.debt, Decimal("0"))
        # Деньги: 500 за новый заказ + 1000 долга.
        self.assertEqual(self._cash_in(), Decimal("1500"))
        # Погашение осталось записью оплаты — с заказом, по которому пришло.
        payment = Payment.objects.get(receipt=old)
        self.assertEqual(payment.amount, Decimal("1000"))
        self.assertIn(str(new.order_number), payment.note)

    def test_new_order_is_not_paid_from_itself(self):
        """Новый заказ в долг + галочка: гасить нечего, долг остаётся его."""
        self._sale(qty=10, paid=Decimal("1000"))        # оплачен, долга нет
        second = self._sale(qty=5, pay_debt=True)       # 500 в долг, платить нечем
        self.assertEqual(second.status_code, 201, second.data)
        new = Receipt.objects.get(id=second.data["id"])
        self.assertEqual(new.debt, Decimal("500"))
        self.assertNotIn("debt_paid", second.data)

    def test_debt_of_other_clients_is_untouched(self):
        other = Client.objects.create(full_name="Азамат", phone="+996555222333")
        r = self.client.post(self.URL, {
            "payment_method": "CASH", "client_id": other.id,
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 3}],
        }, format="json")
        self.assertEqual(r.status_code, 201)
        self._sale(qty=10)                              # долг «нашего» клиента
        self._sale(qty=1, pay_full=True, pay_debt=True)
        self.assertEqual(Receipt.objects.get(id=r.data["id"]).debt, Decimal("300"))

    def test_storekeeper_cannot_collect_debt(self):
        """Деньги за прошлые заказы принимает только админ — как в «Клиентах»."""
        self._sale(qty=10)                              # долг 1000
        self.client.force_authenticate(self.keeper)
        r = self._sale(qty=1, pay_full=True, pay_debt=True)
        self.assertEqual(r.status_code, 403, r.data)
        # Ни заказа, ни погашения: складовщик просто не увидит такой галочки.
        self.assertEqual(Receipt.objects.filter(client=self.client_obj).count(), 1)

    def test_without_the_flag_nothing_is_collected(self):
        first = self._sale(qty=10)
        self._sale(qty=5, pay_full=True)
        self.assertEqual(Receipt.objects.get(id=first.data["id"]).debt, Decimal("1000"))
