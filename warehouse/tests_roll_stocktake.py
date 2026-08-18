"""Акт промера рулона: остаток правится, а расхождение остаётся навсегда.

«Промерил рулеткой, там 10.8 вместо 12.3» — правкой остатка это не проводится:
она приводит число к факту и на этом заканчивается, расхождение исчезает вместе
с причиной. Учёт без объяснения остаётся гипотезой.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from warehouse.models import InventoryLog, Material, Roll, RollStocktake


class RollStocktakeTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="st_admin", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="st_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("1.0"), price_per_pm=Decimal("300"),
        )
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "ROLL", "width": "1.0",
            "length": "12.3", "cost_per_pm": "100", "code": "Р-1",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.roll = Roll.objects.get(material=self.mat)

    def _measure(self, metres, reason="CUTTING", note="", as_user=None):
        self.client.force_authenticate(as_user or self.admin)
        body = {"counted_metres": str(metres), "reason_code": reason}
        if note:
            body["note"] = note
        return self.client.post(
            f"/api/warehouse/rolls/{self.roll.id}/stocktake/", body, format="json"
        )

    # --- сам промер ---
    def test_measuring_less_writes_an_act_and_fixes_the_roll(self):
        """Ровно случай владельца: было 12.3, намерено 10.8."""
        r = self._measure("10.8")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["expected_metres"]), Decimal("12.30"))
        self.assertEqual(Decimal(r.data["counted_metres"]), Decimal("10.80"))
        self.assertEqual(Decimal(r.data["difference"]), Decimal("-1.50"))

        self.roll.refresh_from_db()
        self.assertEqual(self.roll.metres_remaining, Decimal("10.80"))
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.metres_remaining, Decimal("10.80"))

    def test_the_discrepancy_survives_later_sales(self):
        """Главное: акт не пересчитывается из остатков и не тает со временем."""
        self._measure("10.8")
        self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id,
                       "mode": "METER", "quantity": "3"}],
        }, format="json")
        act = RollStocktake.objects.get()
        self.assertEqual(act.expected_metres, Decimal("12.30"))
        self.assertEqual(act.counted_metres, Decimal("10.80"))
        self.assertEqual(act.difference, Decimal("-1.50"))

    def test_surplus_is_recorded_too(self):
        self._measure("12.3")   # ровно, расхождения нет
        act = RollStocktake.objects.get()
        self.assertEqual(act.difference, Decimal("0.00"))

    def test_more_than_received_is_refused(self):
        """Больше, чем приняли, в рулоне быть не может — это ошибка ввода."""
        r = self._measure("20")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("12.30", str(r.data))
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.metres_remaining, Decimal("12.30"))

    # --- причина обязательна ---
    def test_other_without_explanation_is_refused(self):
        r = self._measure("10.8", reason="OTHER")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("note", r.data)

    def test_other_with_explanation_passes(self):
        r = self._measure("10.8", reason="OTHER", note="перемотали на другую втулку")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(RollStocktake.objects.get().note, "перемотали на другую втулку")

    # --- след в системе ---
    def test_stock_journal_shows_the_movement(self):
        """Остаток изменился — значит в ленте движений это обязано быть видно."""
        self._measure("10.8")
        entry = InventoryLog.objects.filter(type=InventoryLog.Type.ADJUSTMENT).latest("id")
        self.assertEqual(entry.quantity_changed, Decimal("-1.5000"))
        self.assertIn("Промер рулона Р-1", entry.reason)

    def test_action_log_explains_what_happened(self):
        self._measure("10.8", reason="SUPPLIER")
        self.assertTrue(
            AuditLog.objects.filter(action__icontains="Промер рулона Р-1").exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action__icontains="Недомер при приёмке").exists()
        )

    # --- права и чтение ---
    def test_storekeeper_cannot_measure(self):
        r = self._measure("10.8", as_user=self.keeper)
        self.assertEqual(r.status_code, 403, r.data)

    def test_acts_are_read_only(self):
        self._measure("10.8")
        act = RollStocktake.objects.get()
        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.client.patch(f"/api/warehouse/roll-stocktakes/{act.id}/",
                              {"difference": "0"}, format="json").status_code, 405
        )
        self.assertEqual(
            self.client.delete(f"/api/warehouse/roll-stocktakes/{act.id}/").status_code, 405
        )

    def test_acts_are_listed_and_filterable_by_material(self):
        self._measure("11")
        self._measure("10.8")
        data = self.client.get(f"/api/warehouse/roll-stocktakes/?material={self.mat.id}").data
        rows = data.get("results", data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["roll_label"], "Р-1")
        self.assertEqual(rows[0]["material_name"], "Оракал")
