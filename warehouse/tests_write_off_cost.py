"""Брак и недостача уходят со склада С СЕБЕСТОИМОСТЬЮ — и из партии.

Аудит 26.09, п. 2: списание штучного материала шло одним числом остатка.
Партия продолжала числить выброшенную штуку, а себестоимость в журнал не
писалась — брак на 800 сом уходил со склада и не появлялся ни в «Списано», ни
в прибыли, только в строке «Не объяснено». Той же болезнью болели промер
рулона и «свести с рулонами»: недостача есть, денег у неё нет.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot, reconcile_with_lots

WRITE_OFF = "/api/warehouse/materials/write-off/"
ADJUST = "/api/warehouse/materials/adjust/"
SUPPLY = "/api/warehouse/materials/supply/"


class PieceWriteOffTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="wo_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.iron = Material.objects.create(
            name="Утюг для борта", unit=Material.Unit.PIECE,
            quantity=Decimal("0"), purchase_price=Decimal("0"),
            price_per_unit=Decimal("1200"),
        )
        r = self.client.post(SUPPLY, {
            "material": self.iron.id, "quantity": "3", "actual_price": "800",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.lot = Roll.objects.get(material=self.iron)

    def _last_log(self, kind):
        return InventoryLog.objects.filter(material=self.iron, type=kind).order_by("-id").first()

    def test_defect_leaves_the_lot_and_writes_its_cost(self):
        """Ровно случай аудита: утюг за 800 браком."""
        r = self.client.post(WRITE_OFF, {
            "material": self.iron.id, "quantity": "1", "reason_code": "DEFECT",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.lot.refresh_from_db()
        self.iron.refresh_from_db()
        self.assertEqual(self.lot.remaining_area, Decimal("2"))
        self.assertEqual(self.iron.quantity, Decimal("2"))
        self.assertEqual(self._last_log(InventoryLog.Type.WRITE_OFF).cost, Decimal("800.00"))

    def test_shortage_on_count_leaves_the_lot_too(self):
        r = self.client.post(ADJUST, {
            "material": self.iron.id, "counted_quantity": "1",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.remaining_area, Decimal("1"))
        self.assertEqual(self._last_log(InventoryLog.Type.ADJUSTMENT).cost, Decimal("1600.00"))

    def test_piece_without_lots_is_priced_from_the_card(self):
        glue = Material.objects.create(
            name="Клей", unit=Material.Unit.PIECE,
            quantity=Decimal("10"), purchase_price=Decimal("17"),
        )
        r = self.client.post(WRITE_OFF, {
            "material": glue.id, "quantity": "2", "reason_code": "DAMAGE",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        log = InventoryLog.objects.get(material=glue, type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(log.cost, Decimal("34.00"))


class RollShortfallCostTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="rs_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.film = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("1.0"), price_per_pm=Decimal("300"),
        )
        # 10 м по 100 сом за метр.
        self.roll = receive_lot(
            self.film, form=Roll.Form.ROLL, width=Decimal("1.0"),
            length=Decimal("10"), purchase_cost=Decimal("1000"), user=self.admin,
        )

    def test_measured_shortfall_carries_the_roll_price(self):
        r = self.client.post(f"/api/warehouse/rolls/{self.roll.id}/stocktake/", {
            "counted_metres": "8.5", "reason_code": "CUTTING",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        log = InventoryLog.objects.filter(
            material=self.film, type=InventoryLog.Type.ADJUSTMENT
        ).latest("id")
        self.assertEqual(log.cost, Decimal("150.00"))

    def test_trimming_the_tail_over_lots_is_priced(self):
        Material.objects.filter(pk=self.film.pk).update(quantity=Decimal("12"))
        self.film.refresh_from_db()
        reconcile_with_lots(self.film, user=self.admin)
        log = InventoryLog.objects.filter(
            material=self.film, type=InventoryLog.Type.ADJUSTMENT
        ).latest("id")
        # Хвост 2 кв.м по последней закупочной (100 за кв.м).
        self.assertEqual(log.cost, Decimal("200.00"))
