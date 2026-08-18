"""Статусы выполнения нельзя двигать по закрытому заказу и нельзя откатывать.

Обе ручки просто присваивали поле и слали уведомление — без единой проверки.
Отменённый и полностью возвращённый заказ становился «готов к выдаче», выданный
откатывался обратно в «готов», а клиенту уходило «✅ ваш заказ выполнен и ждёт
вас на складе» — по заказу, за который ему уже вернули деньги.
"""
from decimal import Decimal
from unittest.mock import patch

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material


class FulfillmentGuardTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="fg_admin", password="x", role=User.Role.ADMIN
        )
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Тахир", phone="+996555000111",
            telegram_chat_id="42",
        )
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.service = PrintingService.objects.create(
            name="Установка", kind=PrintingService.Kind.OTHER,
            base_price=Decimal("100"),
        )
        self.client.force_authenticate(self.admin)

    def _order(self):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "client_id": self.customer.id,
            "items": [
                {"type": "MATERIAL", "material": self.material.id, "quantity": 2},
                {"type": "SERVICE", "service": self.service.id, "quantity": 1},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def _post(self, receipt, action):
        return self.client.post(f"/api/sales/receipts/{receipt.id}/{action}/")

    # ---- закрытый заказ ---------------------------------------------------
    def test_refunded_order_cannot_be_marked_ready(self):
        receipt = self._order()
        self.assertEqual(self._post(receipt, "refund").status_code, 200)

        resp = self._post(receipt, "mark-ready")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("отменён", str(resp.data).lower())
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.PROCESSING
        )

    def test_refunded_order_cannot_be_marked_issued(self):
        receipt = self._order()
        self._post(receipt, "refund")
        self.assertEqual(self._post(receipt, "mark-issued").status_code, 400)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.PROCESSING
        )

    def test_refunded_order_does_not_notify_the_customer(self):
        """Худшая часть дефекта: клиент получал «заказ готов» после возврата."""
        receipt = self._order()
        self._post(receipt, "refund")
        with patch("sales.views.notify_customer") as notify:
            self._post(receipt, "mark-ready")
            self._post(receipt, "mark-issued")
        notify.assert_not_called()

    def test_partial_refund_still_goes_through_production(self):
        """Частичный возврат заказ не закрывает: живые позиции ещё режут."""
        receipt = self._order()
        item = receipt.items.filter(material__isnull=False).first()
        resp = self._post_refund_items(receipt, [item.id])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self._post(receipt, "mark-ready").status_code, 200)

    def _post_refund_items(self, receipt, item_ids):
        return self.client.post(
            f"/api/sales/receipts/{receipt.id}/refund/",
            {"item_ids": item_ids}, format="json",
        )

    # ---- направление перехода --------------------------------------------
    def test_issued_order_cannot_go_back_to_ready(self):
        receipt = self._order()
        self.assertEqual(self._post(receipt, "mark-ready").status_code, 200)
        self.assertEqual(self._post(receipt, "mark-issued").status_code, 200)

        resp = self._post(receipt, "mark-ready")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("выдан", str(resp.data).lower())
        receipt.refresh_from_db()
        self.assertEqual(receipt.fulfillment_status, Receipt.FulfillmentStatus.ISSUED)

    def test_small_order_can_be_issued_straight_from_processing(self):
        """Мелочь отдают сразу, не отмечая готовность отдельным нажатием."""
        receipt = self._order()
        self.assertEqual(self._post(receipt, "mark-issued").status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.fulfillment_status, Receipt.FulfillmentStatus.ISSUED)

    # ---- повторные нажатия ------------------------------------------------
    def test_repeat_press_keeps_200_but_does_not_notify_twice(self):
        receipt = self._order()
        with patch("sales.views.notify_customer") as notify:
            first = self._post(receipt, "mark-ready")
            second = self._post(receipt, "mark-ready")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            notify.call_count, 1, "клиент получил «заказ готов» дважды"
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.fulfillment_status, Receipt.FulfillmentStatus.READY)
