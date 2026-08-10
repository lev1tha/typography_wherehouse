"""Общая выплата (несколько заказов одной суммой) и оплата задним числом.

Покрывает:
  - распределение суммы по заказам от старых к новым, включая частичное закрытие
  - пустая сумма = закрыть выбранные заказы целиком
  - выбор конкретных заказов (`receipt_ids`)
  - переплата не уходит в долг — это сдача
  - дата оплаты: задним числом можно, будущим и кривой — нельзя
  - права: выплата только у админа
  - откат оплаты убирает и записи оплат
  - себестоимость и маржа заказа: видит админ, складовщик — нет
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Payment, Receipt
from sales.sale_service import create_sale
from warehouse.models import Material


class BulkPayTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_bulk", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="keeper_bulk", password="x", role=User.Role.STOREKEEPER
        )
        # Лист за 1000 сом: заказ = 1 лист, чтобы суммы читались глазами.
        self.material = Material.objects.create(
            name="Акрил лист",
            unit=Material.Unit.SQM,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0"),
            purchase_price=Decimal("600"),
            piece_price=Decimal("1000"),
            piece_area=Decimal("2"),
        )
        self.client_one = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Должник", phone="+996700111"
        )
        # Три заказа по 1000 сом, все в долг. Порядок создания = порядок гашения.
        self.r1, self.r2, self.r3 = (self._debt_receipt() for _ in range(3))

    # ---- helpers -----------------------------------------------------------

    def _debt_receipt(self, *, amount_paid=None, client=None):
        return create_sale(
            client=client or self.client_one,
            cashier=self.keeper,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.material, "quantity": 1, "mode": "PIECE"}
            ],
            amount_paid=amount_paid,
        )

    def _pay(self, body, *, user=None):
        self.client.force_authenticate(user or self.admin)
        return self.client.post(
            f"/api/clients/clients/{self.client_one.id}/pay-debt/", body, format="json"
        )

    def _debt(self, receipt):
        receipt.refresh_from_db()
        return receipt.debt

    # ---- распределение суммы ----------------------------------------------

    def test_sum_settles_orders_oldest_first(self):
        """2500 на три заказа по 1000: два закрыты, третий закрыт наполовину."""
        resp = self._pay({"amount": "2500"})
        self.assertEqual(resp.status_code, 200, resp.data)

        self.assertEqual(self._debt(self.r1), Decimal("0"))
        self.assertEqual(self._debt(self.r2), Decimal("0"))
        self.assertEqual(self._debt(self.r3), Decimal("500"))
        self.assertEqual(Decimal(str(resp.data["paid"])), Decimal("2500"))
        self.assertEqual(Decimal(str(resp.data["debt"])), Decimal("500"))
        self.assertEqual(len(resp.data["allocations"]), 3)

    def test_empty_amount_settles_selected_in_full(self):
        """Сумма не задана — закрываем выбранные заказы целиком, без ручного ввода."""
        resp = self._pay({})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Decimal(str(resp.data["paid"])), Decimal("3000"))
        for r in (self.r1, self.r2, self.r3):
            self.assertEqual(self._debt(r), Decimal("0"))
            r.refresh_from_db()
            self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)

    def test_receipt_ids_limit_the_payment(self):
        """Выбран один заказ — деньги уходят только в него, соседние не трогаем."""
        resp = self._pay({"receipt_ids": [str(self.r2.id)]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._debt(self.r1), Decimal("1000"))
        self.assertEqual(self._debt(self.r2), Decimal("0"))
        self.assertEqual(self._debt(self.r3), Decimal("1000"))

    def test_overpayment_is_change_not_debt(self):
        """Принесли больше долга: лишнее — сдача, в минус долг не уходит."""
        resp = self._pay({"amount": "5000"})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Decimal(str(resp.data["paid"])), Decimal("3000"))
        self.assertEqual(Decimal(str(resp.data["change"])), Decimal("2000"))
        self.assertEqual(Decimal(str(resp.data["debt"])), Decimal("0"))
        # Ни один заказ не «переоплачен».
        for r in (self.r1, self.r2, self.r3):
            r.refresh_from_db()
            self.assertEqual(r.amount_paid, r.total_price)

    def test_partial_prepay_counted(self):
        """Предоплата уже принята — добираем только остаток."""
        receipt = self._debt_receipt(amount_paid=Decimal("400"))
        resp = self._pay({"amount": "600", "receipt_ids": [str(receipt.id)]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._debt(receipt), Decimal("0"))
        receipt.refresh_from_db()
        self.assertEqual(receipt.amount_paid, Decimal("1000"))

    def test_no_debt_is_rejected(self):
        self._pay({})  # закрыли всё
        resp = self._pay({"amount": "100"})
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_zero_and_garbage_amounts_are_rejected(self):
        for bad in ("0", "-100", "abc", "NaN", "Infinity"):
            resp = self._pay({"amount": bad})
            self.assertEqual(resp.status_code, 400, f"{bad}: {resp.data}")
        # Долги остались нетронутыми.
        self.assertEqual(self._debt(self.r1), Decimal("1000"))

    # ---- дата оплаты -------------------------------------------------------

    def test_backdated_payment_keeps_its_date(self):
        """Оплата задним числом сохраняет свою дату, а не сегодняшнюю."""
        past = timezone.localdate() - timedelta(days=10)
        resp = self._pay({"amount": "1000", "paid_on": past.isoformat()})
        self.assertEqual(resp.status_code, 200, resp.data)
        payment = Payment.objects.get(receipt=self.r1)
        self.assertEqual(payment.paid_on, past)
        self.assertEqual(payment.amount, Decimal("1000"))
        self.assertEqual(payment.created_by, self.admin)

    def test_future_date_is_rejected(self):
        """Будущим числом оплату не принимаем — этих денег ещё нет."""
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        resp = self._pay({"amount": "1000", "paid_on": future})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(self._debt(self.r1), Decimal("1000"))

    def test_broken_date_is_rejected_not_replaced_by_today(self):
        """Кривую дату отклоняем: молча подменить её сегодняшней — потерять смысл
        оплаты задним числом."""
        resp = self._pay({"amount": "1000", "paid_on": "31-07-2026"})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(Payment.objects.exists())

    def test_default_date_is_today(self):
        self._pay({"amount": "1000"})
        self.assertEqual(Payment.objects.get().paid_on, timezone.localdate())

    def test_payment_method_recorded(self):
        self._pay({"amount": "1000", "method": "MBANK"})
        self.assertEqual(Payment.objects.get().method, "MBANK")

    # ---- права -------------------------------------------------------------

    def test_storekeeper_cannot_pay(self):
        """Деньги — за админом, как и оплата по отдельному чеку."""
        resp = self._pay({"amount": "1000"}, user=self.keeper)
        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertEqual(self._debt(self.r1), Decimal("1000"))

    # ---- запись оплаты и откат --------------------------------------------

    def test_single_receipt_pay_also_records_payment(self):
        """Оплата по одному чеку пишет ту же запись — история не зависит от того,
        каким экраном приняли деньги."""
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f"/api/sales/receipts/{self.r1.id}/pay/",
            {"amount": "400", "paid_on": (timezone.localdate() - timedelta(days=2)).isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        payment = Payment.objects.get(receipt=self.r1)
        self.assertEqual(payment.amount, Decimal("400"))
        self.assertEqual(payment.paid_on, timezone.localdate() - timedelta(days=2))

    def test_unpay_removes_payment_records(self):
        """Откат означает «денег не брали» — оставшаяся запись показывала бы в
        истории клиента платёж, которого нет."""
        self._pay({"amount": "1000"})
        self.assertEqual(Payment.objects.filter(receipt=self.r1).count(), 1)

        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/sales/receipts/{self.r1.id}/unpay/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Payment.objects.filter(receipt=self.r1).count(), 0)
        self.assertEqual(self._debt(self.r1), Decimal("1000"))

    def test_client_card_lists_payments(self):
        past = (timezone.localdate() - timedelta(days=3)).isoformat()
        self._pay({"amount": "1500", "paid_on": past})
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/clients/clients/{self.client_one.id}/")
        self.assertEqual(resp.status_code, 200, resp.data)
        payments = resp.data["payments"]
        self.assertEqual(len(payments), 2)  # 1000 + 500 по двум заказам
        self.assertTrue(all(str(p["paid_on"]) == past for p in payments))


class ReceiptCostVisibilityTests(APITestCase):
    """Себестоимость и маржа заказа: цифры для владельца, не для складовщика."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_cost", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="keeper_cost", password="x", role=User.Role.STOREKEEPER
        )
        self.material = Material.objects.create(
            name="Форекс лист",
            unit=Material.Unit.SQM,
            quantity=Decimal("50"),
            price_per_unit=Decimal("0"),
            purchase_price=Decimal("600"),
            piece_price=Decimal("1000"),
            piece_area=Decimal("1"),
        )
        self.receipt = create_sale(
            client=None,
            cashier=self.keeper,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.material, "quantity": 2, "mode": "PIECE"}
            ],
            amount_paid=Decimal("2000"),
        )

    def _row(self, user):
        self.client.force_authenticate(user)
        resp = self.client.get(f"/api/sales/receipts/{self.receipt.id}/")
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data

    def test_admin_sees_cost_and_margin(self):
        row = self._row(self.admin)
        # 2 листа по 1 кв.м, закупка 600/кв.м → себестоимость 1200, продали за 2000.
        self.assertEqual(Decimal(str(row["cost_total"])), Decimal("1200"))
        self.assertEqual(Decimal(str(row["margin"])), Decimal("800"))

    def test_storekeeper_does_not_see_cost(self):
        row = self._row(self.keeper)
        self.assertIsNone(row["cost_total"])
        self.assertIsNone(row["margin"])
        self.assertIsNone(row["items"][0]["cost_total"])
