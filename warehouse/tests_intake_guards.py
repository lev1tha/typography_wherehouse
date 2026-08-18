"""Правки по ревизии: приход площадного материала и себестоимость сверх партий."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, Roll
from warehouse.rolls import consume_area


class QuickIntakeGuardTests(APITestCase):
    """Быстрый приход поднимал ЧИСЛО, не создавая партии.

    У площадного материала остаток лежит дважды — числом и площадями партий, —
    и после такого прихода они расходились: часть проданного уходила по нулевой
    себестоимости, а возврат чека надувал партии.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="qi_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
        )
        self.piece = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("10"),
            purchase_price=Decimal("4"),
        )

    def test_quick_intake_refuses_an_area_material(self):
        before = self.sheet.quantity
        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": self.sheet.id, "quantity": "20",
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("партией", r.data["detail"])
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, before, "остаток вырос без партии")
        self.assertFalse(Roll.objects.filter(material=self.sheet).exists())

    def test_quick_intake_still_works_for_a_piece_material(self):
        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": self.piece.id, "quantity": "5", "actual_price": "6",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.piece.refresh_from_db()
        self.assertEqual(self.piece.quantity, Decimal("15"))

    def test_lot_intake_is_the_working_path(self):
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.sheet.id, "form": "SHEET",
            "width": "1.22", "height": "2.44", "sheet_count": "3",
            "purchase_cost": "7800",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.sheet.refresh_from_db()
        self.assertEqual(
            self.sheet.quantity,
            Roll.objects.filter(material=self.sheet).aggregate(
                v=Sum("remaining_area")
            )["v"],
            "остаток и партии должны совпадать",
        )


class CostBeyondLotsTests(APITestCase):
    """Остаток сверх партий — штатное состояние (инвентаризация правит число,
    партий не создавая). Но списываться он должен по закупочной цене, а не
    бесплатно: раньше цикл FIFO просто заканчивался и хвост уходил с нулевой
    себестоимостью, завышая маржу."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cb_admin", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Плёнка", unit=Material.Unit.SQM, is_roll_material=True,
            quantity=Decimal("30"), purchase_price=Decimal("100"),
        )
        Roll.objects.create(
            material=self.mat, form=Roll.Form.ROLL,
            initial_area=Decimal("10"), remaining_area=Decimal("10"),
            purchase_cost=Decimal("800"),   # 80 сом/кв.м
        )

    def test_tail_beyond_lots_is_priced_not_free(self):
        with transaction.atomic():
            cogs = consume_area(self.mat, Decimal("20"), user=self.admin)
        # 10 кв.м из партии по 80 + 10 кв.м хвоста по закупочной 100.
        self.assertEqual(cogs, Decimal("800") + Decimal("1000"))
        self.assertNotEqual(cogs, Decimal("800"), "хвост ушёл бесплатно")

    def test_within_lots_still_uses_lot_cost(self):
        with transaction.atomic():
            cogs = consume_area(self.mat, Decimal("4"), user=self.admin)
        self.assertEqual(cogs, Decimal("320"))   # 4 × 80

    def test_stock_value_and_cogs_price_the_tail_the_same_way(self):
        """Две цифры об одном материале должны считаться одинаково."""
        value_before = self.mat.stock_value
        with transaction.atomic():
            cogs = consume_area(self.mat, Decimal("30"), user=self.admin)
        self.assertEqual(cogs.quantize(Decimal("0.01")), value_before)
