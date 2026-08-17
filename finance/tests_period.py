"""Закрытие периода: «по такое-то число трогать больше нельзя».

Отчёт за июль, который владелец уже посмотрел и принял, мог назавтра показать
другую цифру — даты заказов, трат и приходов правятся задним числом, а журнал
действий это записывает, но не останавливает. В 1С месяц закрывают на замок.

Проверяем оба свойства замка: он ДЕРЖИТ (все двери, а не одну) и его можно
снять — осознанно и только админом.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import ExpenseKind, PeriodLock
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material, Supplier


class PeriodLockTests(APITestCase):
    URL = "/api/finance/period/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="pl_admin", password="x", role=User.Role.ADMIN
        )
        self.accountant = User.objects.create_user(
            username="pl_acc", password="x", role=User.Role.ACCOUNTANT
        )
        self.client.force_authenticate(self.admin)
        self.customer = Client.objects.create(full_name="Клиент", phone="+996700000555")
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE,
            quantity=Decimal("500"), price_per_unit=Decimal("18"),
            purchase_price=Decimal("10"),
        )
        self.today = timezone.localdate()
        self.inside = self.today - timedelta(days=30)   # внутри закрытого периода
        self.close_through = self.today - timedelta(days=10)

    def _close(self, through=None):
        return self.client.patch(
            self.URL, {"closed_through": (through or self.close_through).isoformat()},
            format="json",
        )

    def _old_receipt(self):
        """Заказ ВНУТРИ будущего закрытого периода — заводим до закрытия."""
        moment = timezone.make_aware(
            timezone.datetime.combine(self.inside, timezone.datetime.min.time())
        ) + timedelta(hours=12)
        return sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal("1"), "mode": "SQM",
            }],
            amount_paid=Decimal("18"), created_at=moment,
        )

    # ---- сам замок ----------------------------------------------------------
    def test_admin_closes_and_reopens(self):
        self.assertEqual(self._close().status_code, 200)
        self.assertEqual(PeriodLock.load().closed_through, self.close_through)
        reopen = self.client.patch(self.URL, {"closed_through": None}, format="json")
        self.assertEqual(reopen.status_code, 200)
        self.assertIsNone(PeriodLock.load().closed_through)

    def test_future_cannot_be_closed(self):
        """В будущем ещё ничего не произошло — закрывать нечего."""
        resp = self.client.patch(
            self.URL, {"closed_through": (self.today + timedelta(days=1)).isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_accountant_reads_but_cannot_close(self):
        self.client.force_authenticate(self.accountant)
        self.assertEqual(self.client.get(self.URL).status_code, 200)
        self.assertEqual(self._close().status_code, 403)

    def test_closing_is_written_to_the_audit_log(self):
        """Если цифры прошлого месяца всё-таки поехали, по журналу видно, кто
        снял замок."""
        from audit.models import AuditLog

        self._close()
        self.client.patch(self.URL, {"closed_through": None}, format="json")
        actions = list(AuditLog.objects.values_list("action", flat=True))
        self.assertTrue(any("Период закрыт" in a for a in actions), actions)
        self.assertTrue(any("Период ОТКРЫТ" in a for a in actions), actions)

    # ---- что замок держит ---------------------------------------------------
    def test_order_cannot_be_created_inside(self):
        self._close()
        resp = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "amount_paid": 18,
            "order_date": self.inside.isoformat(),
            "items": [{"type": "MATERIAL", "material": self.material.id,
                       "quantity": 1, "mode": "SQM"}],
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("период закрыт", str(resp.data).lower())

    def test_today_still_works(self):
        """Замок закрывает прошлое, а не работу цеха."""
        self._close()
        resp = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "amount_paid": 18,
            "items": [{"type": "MATERIAL", "material": self.material.id,
                       "quantity": 1, "mode": "SQM"}],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_old_order_cannot_be_edited_or_deleted(self):
        receipt = self._old_receipt()
        self._close()
        self.assertEqual(
            self.client.patch(f"/api/sales/receipts/{receipt.id}/",
                              {"title": "правка"}, format="json").status_code, 400
        )
        self.assertEqual(
            self.client.delete(f"/api/sales/receipts/{receipt.id}/").status_code, 400
        )

    def test_change_can_still_be_handed_out_after_closing(self):
        """Сдачу по старому заказу отдать МОЖНО — это деньги клиента.

        Замок держит правки прошлого месяца, а выдача сдачи — движение
        сегодняшнего дня: из кассы уходит сегодня, выручка и прибыль закрытого
        месяца не меняются. Пока проверка тут стояла, «закрыли июль» означало
        «сдачу за июль не отдать», и приходилось открывать весь месяц.
        """
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.material, "quantity": 1, "mode": "SQM"}],
            amount_paid=Decimal("500"),
            created_at=timezone.make_aware(
                timezone.datetime.combine(self.inside, timezone.datetime.min.time())
            ),
        )
        self.assertGreater(receipt.change_due, 0)
        self._close()
        resp = self.client.post(
            f"/api/sales/receipts/{receipt.id}/give-change/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.change_due, Decimal("0"))

    def test_old_order_cannot_be_topped_up_with_more_items(self):
        """Дозаказ — та же правка задним числом: сумма чека и склад поедут.

        Эта дверь была открыта: замок держал правку состава и удаление, а
        «+ Дозаказать» пропускал, и заказ закрытого месяца вырастал с 200 до
        250 сом уже после того, как отчёт приняли.
        """
        receipt = self._old_receipt()
        before = receipt.total_price
        self._close()
        resp = self.client.post(
            f"/api/sales/receipts/{receipt.id}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.material.id,
                        "quantity": 1, "mode": "SQM"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("период закрыт", str(resp.data).lower())
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_price, before)

    def test_old_order_cannot_be_paid_or_refunded(self):
        receipt = self._old_receipt()
        self._close()
        self.assertEqual(
            self.client.post(f"/api/sales/receipts/{receipt.id}/refund/", {},
                             format="json").status_code, 400
        )
        self.assertEqual(
            self.client.post(f"/api/sales/receipts/{receipt.id}/unpay/", {},
                             format="json").status_code, 400
        )

    def test_payment_cannot_be_backdated_into_a_closed_period(self):
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal("1"), "mode": "SQM",
            }],
            amount_paid=Decimal("0"),
        )
        self._close()
        resp = self.client.post(f"/api/sales/receipts/{receipt.id}/pay/", {
            "amount": "10", "paid_on": self.inside.isoformat(),
        }, format="json")
        self.assertEqual(resp.status_code, 400)
        # А сегодняшним днём — пожалуйста.
        self.assertEqual(
            self.client.post(f"/api/sales/receipts/{receipt.id}/pay/",
                             {"amount": "10"}, format="json").status_code, 200
        )

    def test_expense_cannot_be_backdated(self):
        self._close()
        kind = ExpenseKind.objects.first()
        resp = self.client.post("/api/finance/expense-entries/", {
            "kind": kind.id, "amount": "500", "spent_at": self.inside.isoformat(),
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_cash_entry_cannot_be_backdated(self):
        self._close()
        resp = self.client.post("/api/finance/cash/", {
            "account": "CASH", "kind": "IN", "article": "DEPOSIT",
            "amount": "100", "happened_on": self.inside.isoformat(),
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_supply_cannot_be_posted_or_cancelled_inside(self):
        """Приход двигает закуп месяца — в закрытый период его не пускаем, и
        уже проведённый оттуда не отменяем."""
        supplier = Supplier.objects.create(name="Глобал")
        created = self.client.post("/api/warehouse/supplies/", {
            "supplier": supplier.id, "received_on": self.inside.isoformat(),
            "lines": [{"material": self.material.id, "form": "QTY",
                       "quantity": "10", "cost": "1000"}],
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        self._close()
        blocked = self.client.post("/api/warehouse/supplies/", {
            "supplier": supplier.id, "received_on": self.inside.isoformat(),
            "lines": [{"material": self.material.id, "form": "QTY",
                       "quantity": "5", "cost": "500"}],
        }, format="json")
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(
            self.client.delete(f"/api/warehouse/supplies/{created.data['id']}/").status_code, 400
        )

    def test_reopening_lets_the_edit_through(self):
        """Замок снимается — и правка снова возможна: это не бетон, а защита от
        случайной правки закрытого месяца."""
        receipt = self._old_receipt()
        self._close()
        self.assertEqual(
            self.client.patch(f"/api/sales/receipts/{receipt.id}/",
                              {"title": "правка"}, format="json").status_code, 400
        )
        self.client.patch(self.URL, {"closed_through": None}, format="json")
        self.assertEqual(
            self.client.patch(f"/api/sales/receipts/{receipt.id}/",
                              {"title": "правка"}, format="json").status_code, 200
        )

    def test_nothing_is_blocked_while_the_period_is_open(self):
        receipt = self._old_receipt()
        self.assertEqual(
            self.client.patch(f"/api/sales/receipts/{receipt.id}/",
                              {"title": "правка"}, format="json").status_code, 200
        )
