"""Гашение долга в кассе — из тех же денег, что принесли (аудит 2026-08-18, п. 3).

Подсказка в кассе говорила «Возьмём с клиента 2 160 — заказ и долг вместе», а
поле называлось «Платит сейчас». Кассир вписывал 2 160 — и сумма сверх заказа
становилась СДАЧЕЙ (2 110), а долги закрывались отдельно и целиком: долг
погашен, у клиента «сдача» на ту же сумму, касса записала 4 270 при 2 160 в
ящике, а следующий заказ клиента закрылся этой сдачей бесплатно.

Теперь `amount_paid` при `pay_debt` — всё, что клиент принёс: сначала заказ,
остаток — долги от старых к новым, что не пригодилось — сдача. «Вся сумма» —
заказ и долги целиком. Пустая сумма без «Вся сумма» — отказ: гасить нечем.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Payment, Receipt
from warehouse.models import Material


class PayDebtWithBroughtMoneyTests(APITestCase):
    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(username="pda_admin", password="x", role=User.Role.ADMIN)
        self.client_obj = Client.objects.create(full_name="Азат", phone="+996772009031")
        self.paper = Material.objects.create(
            name="Бумага", unit=Material.Unit.PIECE, quantity=Decimal("10000"),
            price_per_unit=Decimal("50"), purchase_price=Decimal("30"),
        )
        self.client.force_authenticate(self.admin)

    def _sale(self, *, qty, paid=None, pay_debt=False, pay_full=False, use_change=False):
        body = {
            "payment_method": "CASH", "client_id": self.client_obj.id,
            "items": [{"type": "MATERIAL", "material": self.paper.id, "quantity": qty}],
        }
        if paid is not None:
            body["amount_paid"] = str(paid)
        if pay_full:
            body["pay_full"] = True
        if pay_debt:
            body["pay_debt"] = True
        if use_change:
            body["use_change"] = True
        return self.client.post(self.URL, body, format="json")

    def _cash_in(self):
        return sum((e.amount for e in CashEntry.objects.filter(kind=CashEntry.Kind.IN)), Decimal("0"))

    def _client_change(self):
        return sum((r.change_due for r in self.client_obj.receipts.all()), Decimal("0"))

    def _client_debt(self):
        return sum((r.debt for r in self.client_obj.receipts.all()), Decimal("0"))

    # --- главный случай: вписали заказ + долг ---------------------------------

    def test_full_amount_typed_in_closes_debt_without_phantom_change(self):
        old = self._sale(qty=40)                          # 2 000 в долг
        self.assertEqual(Decimal(old.data["debt"]), Decimal("2000"))
        cash0 = self._cash_in()

        r = self._sale(qty=1, paid=Decimal("2050"), pay_debt=True)   # заказ 50 + долг 2 000
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["debt_paid"])), Decimal("2000"))
        new = Receipt.objects.get(pk=r.data["id"])
        self.assertEqual(new.amount_paid, Decimal("50"))
        self.assertEqual(new.change_due, Decimal("0"))          # никакой «сдачи» на 2 000
        self.assertEqual(new.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(self._client_debt(), Decimal("0"))
        self.assertEqual(self._client_change(), Decimal("0"))
        # Касса: ровно принесённые 2 050, а не 2 050 + 2 000.
        self.assertEqual(self._cash_in() - cash0, Decimal("2050"))
        payment = Payment.objects.get(receipt_id=old.data["id"])
        self.assertEqual(payment.amount, Decimal("2000"))
        self.assertIn(str(new.order_number), payment.note)

    def test_partial_amount_pays_the_order_first_then_oldest_debt(self):
        first = self._sale(qty=20)                        # 1 000 в долг (старый)
        second = self._sale(qty=10)                       # 500 в долг
        r = self._sale(qty=1, paid=Decimal("1250"), pay_debt=True)  # 50 заказ + 1 200 в долги
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["debt_paid"])), Decimal("1200"))
        new = Receipt.objects.get(pk=r.data["id"])
        self.assertEqual(new.amount_paid, Decimal("50"))
        self.assertEqual(new.change_due, Decimal("0"))
        # Старший долг закрыт целиком, младший — на остаток.
        self.assertEqual(Receipt.objects.get(pk=first.data["id"]).debt, Decimal("0"))
        self.assertEqual(Receipt.objects.get(pk=second.data["id"]).debt, Decimal("300"))
        self.assertEqual(self._client_debt(), Decimal("300"))

    def test_more_than_everything_leaves_change_on_the_new_receipt(self):
        self._sale(qty=20)                                # долг 1 000
        r = self._sale(qty=1, paid=Decimal("1200"), pay_debt=True)  # 50 + 1 000 + 150 лишних
        new = Receipt.objects.get(pk=r.data["id"])
        self.assertEqual(Decimal(str(r.data["debt_paid"])), Decimal("1000"))
        self.assertEqual(new.change_due, Decimal("150"))
        self.assertEqual(self._client_debt(), Decimal("0"))
        # Касса — ровно принесённое: 50 + 150 сдачи на новом чеке + 1 000 долга.
        self.assertEqual(self._cash_in(), Decimal("1200"))

    def test_less_than_the_order_leaves_old_debt_untouched(self):
        old = self._sale(qty=20)                          # долг 1 000
        r = self._sale(qty=10, paid=Decimal("300"), pay_debt=True)  # заказ 500, принёс 300
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["debt_paid"])), Decimal("0"))
        new = Receipt.objects.get(pk=r.data["id"])
        self.assertEqual(new.amount_paid, Decimal("300"))
        self.assertEqual(new.debt, Decimal("200"))
        self.assertEqual(Receipt.objects.get(pk=old.data["id"]).debt, Decimal("1000"))

    # --- «вся сумма» и пустое поле ------------------------------------------

    def test_pay_full_still_closes_order_and_all_debts(self):
        old = self._sale(qty=20)
        r = self._sale(qty=1, pay_full=True, pay_debt=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["debt_paid"])), Decimal("1000"))
        self.assertEqual(Receipt.objects.get(pk=old.data["id"]).debt, Decimal("0"))
        new = Receipt.objects.get(pk=r.data["id"])
        self.assertEqual(new.change_due, Decimal("0"))
        self.assertEqual(self._cash_in(), Decimal("1050"))

    def test_empty_amount_with_pay_debt_is_refused_when_there_is_debt(self):
        self._sale(qty=20)                                # долг 1 000
        r = self._sale(qty=1, pay_debt=True)              # сумма пустая, «вся сумма» не нажата
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("сколько принёс", r.data["detail"])
        self.assertEqual(self._client_debt(), Decimal("1000"))
        self.assertEqual(Receipt.objects.filter(client=self.client_obj).count(), 1)

    def test_empty_amount_with_pay_debt_but_no_debt_is_just_a_sale(self):
        r = self._sale(qty=1, pay_debt=True)              # долгов нет — галочка ни на что
        self.assertEqual(r.status_code, 201, r.data)
        self.assertNotIn("debt_paid", r.data)

    # --- вместе с зачётом сдачи ----------------------------------------------

    def test_change_offset_and_debt_together(self):
        """Заказ 500, у клиента сдача 100 с прошлого раза и долг 1 000. Принёс
        1 400: зачли 100 сдачи, заказ закрыт (400 деньгами), 1 000 в долг."""
        first = self._sale(qty=20)                        # долг 1 000
        second = self._sale(qty=1, paid=Decimal("150"))   # заказ 50, принёс 150 → сдача 100
        self.assertEqual(Receipt.objects.get(pk=second.data["id"]).change_due, Decimal("100"))
        cash0 = self._cash_in()

        r = self._sale(qty=10, paid=Decimal("1400"), pay_debt=True, use_change=True)
        self.assertEqual(r.status_code, 201, r.data)
        new = Receipt.objects.get(pk=r.data["id"])
        self.assertEqual(new.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(Decimal(str(r.data["debt_paid"])), Decimal("1000"))
        self.assertEqual(Receipt.objects.get(pk=first.data["id"]).debt, Decimal("0"))
        # Сдача либо зачлась в заказ, либо осталась — но не удвоилась и не
        # превратилась в долг: всего у клиента 100 (или зачтено), денег принесли
        # 1 400, в кассу легло 1 400.
        self.assertEqual(self._cash_in() - cash0, Decimal("1400"))
        self.assertEqual(self._client_debt(), Decimal("0"))
        self.assertLessEqual(self._client_change(), Decimal("100"))
