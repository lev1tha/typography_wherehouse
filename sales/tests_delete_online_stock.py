"""Удаление неоплаченного онлайн-заказа не должно дорисовывать склад.

Наличный заказ списывает материал при оформлении, онлайн — только после
подтверждения оплаты. Удаление возвращало на склад каждую строку, не глядя на
то, уходила ли она оттуда: брошенный онлайн-счёт поднимал остаток на своё
количество, а стоимость склада — на его цену. Повторять можно было сколько
угодно, а висящие неоплаченные счета админ вычищает как раз пачками.
"""
from decimal import Decimal

from django.db.models import Sum
from rest_framework.test import APITestCase

from accounts.models import User
from sales import sale_service
from sales.models import Receipt, TransactionItem
from warehouse.models import Material, Roll


class DeleteUnpaidOnlineOrderTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="do_admin", password="x", role=User.Role.ADMIN
        )
        self.piece = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.client.force_authenticate(self.admin)

    def _order(self, method, qty="4"):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": method,
            **({"pay_full": True} if method != "ONLINE" else {}),
            "items": [{"type": "MATERIAL", "material": self.piece.id, "quantity": qty}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def _qty(self):
        self.piece.refresh_from_db()
        return self.piece.quantity

    def test_deleting_unpaid_online_order_leaves_stock_alone(self):
        before = self._qty()
        receipt = self._order("ONLINE")
        self.assertFalse(receipt.stock_deducted)
        self.assertEqual(self._qty(), before, "онлайн-счёт не должен трогать склад")

        resp = self.client.delete(f"/api/sales/receipts/{receipt.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(
            self._qty(), before, "удаление дорисовало на склад то, чего не брали"
        )

    def test_repeated_abandoned_orders_do_not_inflate_stock(self):
        """Счёт брошен и удалён десять раз — склад стоит на месте."""
        before = self._qty()
        for _ in range(10):
            receipt = self._order("ONLINE", qty="3")
            self.client.delete(f"/api/sales/receipts/{receipt.id}/")
        self.assertEqual(self._qty(), before)

    def test_deleting_a_paid_online_order_still_returns_the_material(self):
        """А вот оплаченный онлайн-заказ склад списал — возврат обязателен."""
        before = self._qty()
        receipt = self._order("ONLINE")
        sale_service.confirm_payment(receipt)
        self.assertEqual(self._qty(), before - Decimal("4"))

        self.client.delete(f"/api/sales/receipts/{receipt.id}/")
        self.assertEqual(self._qty(), before)

    def test_deleting_a_cash_order_still_returns_the_material(self):
        """Обычная продажа — поведение не изменилось."""
        before = self._qty()
        receipt = self._order("CASH")
        self.assertEqual(self._qty(), before - Decimal("4"))

        self.client.delete(f"/api/sales/receipts/{receipt.id}/")
        self.assertEqual(self._qty(), before)


class DeleteUnpaidOnlineRollTests(APITestCase):
    """То же для площадного материала: партии FIFO тоже не должны расти."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="dor_admin", password="x", role=User.Role.ADMIN
        )
        self.sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"),
        )
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.sheet.id, "form": "SHEET",
            "width": "1.22", "height": "2.44", "sheet_count": "3",
            "purchase_cost": "7800",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def _state(self):
        self.sheet.refresh_from_db()
        parts = Roll.objects.filter(material=self.sheet).aggregate(
            v=Sum("remaining_area")
        )["v"]
        return self.sheet.quantity, parts

    def test_deleting_unpaid_online_order_touches_neither_stock_nor_lots(self):
        before = self._state()
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "ONLINE",
            "items": [{"type": "MATERIAL", "material": self.sheet.id, "mode": "SQM", "quantity": "2"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(self._state(), before)

        self.client.delete(f"/api/sales/receipts/{r.data['id']}/")
        after = self._state()
        self.assertEqual(after, before, "остаток или партии выросли из ничего")
        # Стоимость склада — та же цифра, что видна в «Обзоре».
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, before[0])
