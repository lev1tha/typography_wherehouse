"""Статусы выполнения нельзя двигать по ЗАКРЫТОМУ заказу; по живому — можно куда угодно.

Обе ручки просто присваивали поле и слали уведомление — без единой проверки.
Отменённый и полностью возвращённый заказ становился «готов к выдаче», а клиенту
уходило «✅ ваш заказ выполнен и ждёт вас на складе» — по заказу, за который ему
уже вернули деньги. Эта половина проверок осталась и останется.

А вот запрет ОТКАТА снят (решение владельца, 2026-08-27). Кнопку нажимают
руками и промахиваются: «Выдан» был тупиком, из которого заказ нельзя было
вернуть ничем, кроме удаления, и цех оставался с заказом, который числится у
клиента, а лежит на полке. Теперь ход разрешён в любую сторону, а клиенту на
откат уходит ПОПРАВКА, а не второе «ваш заказ готов»: он выйдет из дома зря,
если ему не сказать.
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
    def test_issued_order_can_go_back_to_ready(self):
        """Промах по «Выдан» исправляется, а не хоронит заказ.

        До 27.08 здесь стоял 400: «товар у клиента». Но нажатие по ошибке
        означает ровно обратное — товар НЕ у клиента, он на полке.
        """
        receipt = self._order()
        self.assertEqual(self._post(receipt, "mark-issued").status_code, 200)

        resp = self._post(receipt, "mark-ready")
        self.assertEqual(resp.status_code, 200, resp.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.fulfillment_status, Receipt.FulfillmentStatus.READY)

    def test_ready_order_can_go_back_to_processing(self):
        """Готовность отметили раньше времени — вернуть в работу."""
        receipt = self._order()
        self.assertEqual(self._post(receipt, "mark-ready").status_code, 200)

        resp = self._post(receipt, "mark-processing")
        self.assertEqual(resp.status_code, 200, resp.data)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.PROCESSING
        )

    def test_issued_order_can_go_all_the_way_back(self):
        receipt = self._order()
        self._post(receipt, "mark-issued")
        self.assertEqual(self._post(receipt, "mark-processing").status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.PROCESSING
        )

    # ---- что уходит клиенту при откате ------------------------------------
    def test_rollback_sends_a_correction_not_a_second_ready(self):
        """Главное в откате: клиент должен узнать, что идти пока НЕ надо."""
        receipt = self._order()
        self._post(receipt, "mark-ready")
        with patch("sales.views.notify_customer") as notify:
            self._post(receipt, "mark-processing")
        notify.assert_called_once()
        text = notify.call_args.args[1]
        self.assertIn("ещё в работе", text)
        self.assertNotIn("ждёт вас на складе", text)

    def test_rollback_from_issued_says_the_issue_mark_was_a_mistake(self):
        receipt = self._order()
        self._post(receipt, "mark-issued")
        with patch("sales.views.notify_customer") as notify:
            self._post(receipt, "mark-ready")
        text = notify.call_args.args[1]
        self.assertIn("ошибк", text.lower())

    def test_forward_move_keeps_its_old_message(self):
        """Откат не должен был испортить обычный ход вперёд."""
        receipt = self._order()
        with patch("sales.views.notify_customer") as notify:
            self._post(receipt, "mark-ready")
        self.assertIn("ждёт вас на складе", notify.call_args.args[1])

    def test_rollback_is_written_down_with_both_statuses(self):
        """По журналу должно быть видно, кто и откуда куда вернул заказ."""
        from audit.models import AuditLog

        receipt = self._order()
        self._post(receipt, "mark-issued")
        self._post(receipt, "mark-processing")
        entry = AuditLog.objects.order_by("-id").first()
        self.assertIn("Выдан", entry.action)
        self.assertIn("Готовится", entry.action)
        self.assertIn("откат", entry.action)

    # ---- закрытый заказ откатывать тоже нельзя ----------------------------
    def test_refunded_order_cannot_be_rolled_back_either(self):
        """Снятие запрета на откат не открыло дорогу к возвращённому заказу."""
        receipt = self._order()
        self._post(receipt, "mark-ready")
        self._post(receipt, "refund")

        resp = self._post(receipt, "mark-processing")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("отменён", str(resp.data).lower())

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


class FulfillmentRollbackAccessTests(APITestCase):
    """Кому разрешён откат и как его просят одной ручкой.

    Откат — исправление ошибки цеха, а не денежная операция: его делает тот,
    кто у кассы стоит, иначе за каждым промахом по кнопке придётся звать
    администратора. Бухгалтеру закрыто всё, что пишет, — откат тоже.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="fr_admin", password="x", role=User.Role.ADMIN
        )
        self.storekeeper = User.objects.create_user(
            username="fr_store", password="x", role=User.Role.STOREKEEPER
        )
        self.accountant = User.objects.create_user(
            username="fr_acc", password="x", role=User.Role.ACCOUNTANT
        )
        self.material = Material.objects.create(
            name="Профиль", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.service = PrintingService.objects.create(
            name="Сборка", kind=PrintingService.Kind.OTHER, base_price=Decimal("100"),
        )

    def _order(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "SERVICE", "service": self.service.id, "quantity": 1}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def _as(self, user, receipt, action, body=None):
        self.client.force_authenticate(user)
        return self.client.post(
            f"/api/sales/receipts/{receipt.id}/{action}/", body or {}, format="json"
        )

    # ---- кто может ---------------------------------------------------------
    def test_storekeeper_can_roll_the_status_back(self):
        receipt = self._order()
        self.assertEqual(self._as(self.storekeeper, receipt, "mark-issued").status_code, 200)
        self.assertEqual(self._as(self.storekeeper, receipt, "mark-processing").status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.PROCESSING
        )

    def test_accountant_cannot_touch_the_status(self):
        receipt = self._order()
        self._as(self.admin, receipt, "mark-ready")
        for action in ("mark-processing", "mark-ready", "mark-issued"):
            self.assertEqual(
                self._as(self.accountant, receipt, action).status_code, 403, action
            )
        receipt.refresh_from_db()
        self.assertEqual(receipt.fulfillment_status, Receipt.FulfillmentStatus.READY)

    # ---- одна ручка вместо трёх -------------------------------------------
    def test_set_fulfillment_moves_to_any_status(self):
        receipt = self._order()
        for wanted in ("ISSUED", "PROCESSING", "READY"):
            resp = self._as(self.admin, receipt, "set-fulfillment", {"status": wanted})
            self.assertEqual(resp.status_code, 200, resp.data)
            receipt.refresh_from_db()
            self.assertEqual(receipt.fulfillment_status, wanted)

    def test_set_fulfillment_rejects_a_status_that_does_not_exist(self):
        """Опечатка в статусе — отказ, а не молчаливое «ничего не поменялось»."""
        receipt = self._order()
        resp = self._as(self.admin, receipt, "set-fulfillment", {"status": "ГОТОВО"})
        self.assertEqual(resp.status_code, 400, resp.data)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.PROCESSING
        )

    def test_set_fulfillment_without_a_status_is_rejected(self):
        receipt = self._order()
        self.assertEqual(
            self._as(self.admin, receipt, "set-fulfillment").status_code, 400
        )

    def test_set_fulfillment_repeat_does_not_notify_twice(self):
        receipt = self._order()
        with patch("sales.views.notify_customer") as notify:
            self._as(self.admin, receipt, "set-fulfillment", {"status": "READY"})
            self._as(self.admin, receipt, "set-fulfillment", {"status": "READY"})
        self.assertLessEqual(notify.call_count, 1)
