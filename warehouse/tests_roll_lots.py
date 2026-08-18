"""Партия рулона: ширина заморожена, метры считаются от неё, недолив виден.

Ширину нельзя читать из карточки материала: правка опечатки «0.9 → 1.0» в
справочнике молча пересчитала бы остатки всех рулонов, включая закрытые. И под
одной карточкой законно лежит оракал 1.0, 1.26 и 1.52 — общей ширины у него
просто нет.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from warehouse.models import Material, Roll


class RollWidthFrozenTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="rl_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("1.0"), price_per_pm=Decimal("300"),
        )

    def _receive(self, length, width=None, **extra):
        body = {"material": self.mat.id, "form": "ROLL", "length": str(length), **extra}
        if width is not None:
            body["width"] = str(width)
        body.setdefault("purchase_cost", "1000")
        r = self.client.post("/api/warehouse/materials/receive-roll/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Roll.objects.filter(material=self.mat).order_by("-id").first()

    # --- ширина замораживается ---
    def test_width_defaults_from_the_card_and_freezes(self):
        roll = self._receive(50)
        self.assertEqual(roll.width, Decimal("1.00"))

    def test_editing_the_card_does_not_move_existing_lots(self):
        """Главное: опечатку в справочнике поправили — остатки не поехали."""
        self._receive(50)
        self.mat.refresh_from_db()
        before = self.mat.metres_remaining
        self.assertEqual(before, Decimal("50.00"))

        self.client.patch(f"/api/warehouse/materials/{self.mat.id}/",
                          {"roll_width": "1.52"}, format="json")
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.metres_remaining, before,
                         "правка ширины в карточке пересчитала принятый рулон")

    def test_one_card_holds_rolls_of_different_widths(self):
        """Оракал 1.0, 1.26 и 1.52 живут под одной карточкой."""
        self._receive(50, width="1.0")
        self._receive(20, width="1.26")
        self._receive(10, width="1.52")
        self.mat.refresh_from_db()
        # Метры складываются как метры, а не через общую ширину.
        self.assertEqual(self.mat.metres_remaining, Decimal("80.00"))
        # А площадь — своя у каждого.
        self.assertEqual(
            self.mat.quantity,
            Decimal("50") * Decimal("1.0")
            + Decimal("20") * Decimal("1.26")
            + Decimal("10") * Decimal("1.52"),
        )

    def test_selling_across_lots_uses_each_lot_width(self):
        """55 м: 50 из первого рулона (1.0) и 5 из второго (1.26)."""
        self._receive(50, width="1.0")
        self._receive(20, width="1.26")
        before = Material.objects.get(pk=self.mat.pk).quantity
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id,
                       "mode": "METER", "quantity": "55"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.mat.refresh_from_db()
        # Ушло 50×1.0 + 5×1.26 = 56.3 кв.м, а не 55 × какая-то одна ширина.
        self.assertEqual(before - self.mat.quantity, Decimal("56.30"))
        self.assertEqual(self.mat.metres_remaining, Decimal("15.00"))

    def test_selling_more_metres_than_there_are_is_refused(self):
        self._receive(10, width="1.0")
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id,
                       "mode": "METER", "quantity": "12"}],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("пог.м", str(r.data))

    def test_refund_returns_metres_to_the_same_lot(self):
        self._receive(50, width="1.0")
        before = Material.objects.get(pk=self.mat.pk).quantity
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id,
                       "mode": "METER", "quantity": "4"}],
        }, format="json")
        self.client.post(f"/api/sales/receipts/{r.data['id']}/refund/", {}, format="json")
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, before)
        self.assertEqual(self.mat.metres_remaining, Decimal("50.00"))


class RollIntakeInMetresTests(APITestCase):
    """Приход рулона в метрах: цену за метр не делим в уме, недолив виден."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ri_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Туника", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("0.9"),
        )

    def _post(self, **body):
        return self.client.post("/api/warehouse/materials/receive-roll/",
                                {"material": self.mat.id, "form": "ROLL", **body},
                                format="json")

    def test_price_per_metre_is_enough(self):
        """12 000 ÷ 45 = 266.67 в уме считать не нужно."""
        r = self._post(length="50", cost_per_pm="240")
        self.assertEqual(r.status_code, 201, r.data)
        roll = Roll.objects.get(material=self.mat)
        self.assertEqual(roll.purchase_cost, Decimal("12000.00"))
        self.assertEqual(roll.cost_per_pm, Decimal("240.00"))

    def test_total_cost_still_works(self):
        r = self._post(length="50", purchase_cost="12000")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Roll.objects.get(material=self.mat).cost_per_pm, Decimal("240.00"))

    def test_neither_price_is_refused(self):
        r = self._post(length="50")
        self.assertEqual(r.status_code, 400, r.data)

    def test_shortfall_is_recorded_and_logged(self):
        """Заявлено 50, намерили 48.6 — недостача видна цифрой."""
        r = self._post(length="48.6", declared_length="50", cost_per_pm="240")
        self.assertEqual(r.status_code, 201, r.data)
        roll = Roll.objects.get(material=self.mat)
        self.assertEqual(roll.declared_length, Decimal("50.00"))
        self.assertEqual(roll.length, Decimal("48.60"))
        self.assertEqual(roll.shortfall, Decimal("1.40"))
        # Платим за принятое, а не за заявленное.
        self.assertEqual(roll.purchase_cost, Decimal("11664.00"))
        self.assertTrue(
            AuditLog.objects.filter(action__icontains="недостача").exists(),
            "недолив не попал в журнал действий",
        )

    def test_no_shortfall_when_it_matches(self):
        self._post(length="50", declared_length="50", cost_per_pm="240")
        self.assertEqual(Roll.objects.get(material=self.mat).shortfall, Decimal("0.00"))
