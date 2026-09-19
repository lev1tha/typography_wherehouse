"""Недостача по инвентаризации — это ДЕНЬГИ, и цифру нужно сохранить.

У площадного материала себестоимость ушедшего считает FIFO по партиям, а
штучный уходил с пустым `cost`: 4 000 диодов, списанных правкой остатка на
проде, стоили 14 000 сомов, и в отчётах эти деньги просто исчезали — склад
худел, а ни расхода, ни себестоимости не появлялось.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import InventoryLog, Material


class AdjustmentCostTests(APITestCase):
    URL = "/api/warehouse/materials/adjust/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ac_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.piece = Material.objects.create(
            name="Диод", unit=Material.Unit.PIECE,
            quantity=Decimal("500"), purchase_price=Decimal("4"),
        )

    def _adjust(self, counted):
        r = self.client.post(
            self.URL, {"material": self.piece.id, "counted_quantity": counted},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        return InventoryLog.objects.filter(
            material=self.piece, type=InventoryLog.Type.ADJUSTMENT
        ).latest("id")

    def test_shortage_keeps_its_cost(self):
        log = self._adjust("100")
        self.assertEqual(log.quantity_changed, Decimal("-400"))
        self.assertEqual(log.cost, Decimal("1600.00"))

    def test_surplus_has_no_cost(self):
        """Прибавка — не потеря: денег по ней не уходило, выдумывать нечего."""
        log = self._adjust("700")
        self.assertEqual(log.quantity_changed, Decimal("200"))
        self.assertIsNone(log.cost)

    def test_material_with_a_zero_purchase_price_gives_zero_not_a_crash(self):
        """Закупочной цены может не быть вовсе (ноль) — это не повод падать."""
        self.piece.purchase_price = Decimal("0")
        self.piece.save(update_fields=["purchase_price"])
        log = self._adjust("100")
        self.assertEqual(log.cost, Decimal("0.00"))
