"""Партия = физический рулон: из какого режем, в тот и возвращаем.

«Остаток 2.9 пог.м» — это не один рулон, а два: початый на 0.9 м и целый на
2.0 м. Мастеру нужно знать, какой он берёт, и что початый надо дожечь первым.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from warehouse.models import Material, Roll


class RollChoiceTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="rc2_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("1.0"), price_per_pm=Decimal("300"),
        )
        # Початый дешёвый (пришёл раньше) и целый дорогой.
        self.started = self._receive("0.9", cost="18", code="Р-1")     # 20 сом/пог.м
        self.fresh = self._receive("2.0", cost="400", code="Р-2")      # 200 сом/пог.м

    def _receive(self, length, cost, code):
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "ROLL", "width": "1.0",
            "length": length, "purchase_cost": cost, "code": code,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Roll.objects.filter(material=self.mat).order_by("-id").first()

    def _sell(self, metres, roll=None):
        item = {"type": "MATERIAL", "material": self.mat.id,
                "mode": "METER", "quantity": str(metres)}
        if roll is not None:
            item["roll"] = roll.id
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "items": [item],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def _left(self, roll):
        roll.refresh_from_db()
        return roll.metres_remaining

    # --- по умолчанию дожигаем початый ---
    def test_without_a_choice_the_started_roll_goes_first(self):
        receipt = self._sell("0.5")
        self.assertEqual(self._left(self.started), Decimal("0.40"))
        self.assertEqual(self._left(self.fresh), Decimal("2.00"))
        # И себестоимость — этого рулона, дешёвого.
        self.assertEqual(receipt.items.get().cost_total, Decimal("10.00"))

    # --- мастер выбрал целый ---
    def test_choosing_a_roll_cuts_from_it(self):
        receipt = self._sell("0.5", roll=self.fresh)
        self.assertEqual(self._left(self.started), Decimal("0.90"), "початый не должен был тронуться")
        self.assertEqual(self._left(self.fresh), Decimal("1.50"))
        # Себестоимость — дорогого рулона: 0.5 м × 200 сом/пог.м.
        self.assertEqual(receipt.items.get().cost_total, Decimal("100.00"))

    def test_the_chosen_roll_is_remembered_on_the_line(self):
        receipt = self._sell("0.5", roll=self.fresh)
        item = receipt.items.get()
        self.assertEqual(item.roll_id, self.fresh.id)
        data = self.client.get(f"/api/sales/receipts/{receipt.id}/").data
        self.assertEqual(data["items"][0]["roll_label"], "Р-2")

    def test_a_long_cut_continues_into_the_next_roll(self):
        """В цехе рулон кончается посреди заказа — режем дальше со следующего."""
        receipt = self._sell("1.5")
        self.assertEqual(self._left(self.started), Decimal("0.00"))
        self.assertEqual(self._left(self.fresh), Decimal("1.40"))
        # 0.9 м по 20 + 0.6 м по 200 = 18 + 120.
        self.assertEqual(receipt.items.get().cost_total, Decimal("138.00"))

    # --- возврат ---
    def test_refund_returns_metres_to_the_same_roll(self):
        receipt = self._sell("0.5", roll=self.fresh)
        self.client.post(f"/api/sales/receipts/{receipt.id}/refund/", {}, format="json")
        self.assertEqual(self._left(self.fresh), Decimal("2.00"))
        self.assertEqual(self._left(self.started), Decimal("0.90"),
                         "метры вернулись в чужой рулон")

    def test_deleting_the_receipt_also_returns_to_the_same_roll(self):
        receipt = self._sell("0.5", roll=self.fresh)
        self.client.delete(f"/api/sales/receipts/{receipt.id}/")
        self.assertEqual(self._left(self.fresh), Decimal("2.00"))
        self.assertEqual(self._left(self.started), Decimal("0.90"))

    # --- остаток материала не разъезжается ---
    def test_material_total_matches_the_sum_of_rolls(self):
        self._sell("0.4", roll=self.fresh)
        self._sell("0.3")
        self.mat.refresh_from_db()
        total = sum(
            (r.metres_remaining for r in Roll.objects.filter(material=self.mat)),
            Decimal("0"),
        )
        self.assertEqual(self.mat.metres_remaining, total)
