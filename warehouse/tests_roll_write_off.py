"""Списание рулона — по рулону и в метрах (аудит 2026-08-18, п. 7).

Общее списание рулонного материала шло числом в кв.м и FIFO со старейшего
рулона: «порвали 2 м рулона №8» вводили как «2» — это 2 кв.м = 1.67 м, и они
уходили с целого рулона №7, а №8 терял лишь остаток. Остаток обоих рулонов
после этого врал, себестоимость списания бралась не от того рулона (171 сом
вместо 400). Теперь брак списывается с конкретного рулона, в метрах, его
шириной и по его цене; общее списание рулонного материала в кв.м закрыто.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class RollWriteOffTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="rw_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="rw_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Акрил 8 мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"),
            price_per_pm=Decimal("1000"),
        )
        # Старый початый рулон дешёвый, новый целый — дорогой: раньше списание
        # «с рулона №8» уходило именно с №7.
        self.old = receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1.2"), length=Decimal("0.9"),
            purchase_cost=Decimal("18"),
        )
        self.new = receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1.2"), length=Decimal("2"),
            purchase_cost=Decimal("400"),
        )
        self.mat.refresh_from_db()

    def _write_off(self, roll, metres, **extra):
        return self.client.post(
            f"/api/warehouse/rolls/{roll.pk}/write-off/",
            {"metres": str(metres), "reason_code": "DAMAGE", "note": "порвали", **extra},
            format="json",
        )

    def test_write_off_takes_metres_from_that_roll_only(self):
        r = self._write_off(self.new, 2)
        self.assertEqual(r.status_code, 200, r.data)
        self.old.refresh_from_db()
        self.new.refresh_from_db()
        self.mat.refresh_from_db()
        self.assertEqual(self.old.remaining_area, Decimal("1.0800"))   # цел
        self.assertEqual(self.new.remaining_area, Decimal("0"))        # 2 м × 1.2 = 2.4 кв.м ушли
        self.assertEqual(self.mat.quantity, Decimal("1.0800"))
        self.assertEqual(self.mat.metres_remaining, Decimal("0.90"))
        log = InventoryLog.objects.filter(material=self.mat, type=InventoryLog.Type.WRITE_OFF).get()
        self.assertEqual(log.quantity_changed, Decimal("-2.4000"))
        self.assertIn("Порча", log.reason)
        self.assertIn(f"№{self.new.pk}", log.reason)
        self.assertIn("2 м", log.reason)
        # Себестоимость списанного — по ЭТОМУ рулону: 2 м × 200 = 400, а не 171.
        entry = AuditLog.objects.order_by("-id").first()
        self.assertIn("400.00 сом", entry.action)

    def test_partial_write_off_keeps_the_rest_of_the_roll(self):
        r = self._write_off(self.new, "0.5")
        self.assertEqual(r.status_code, 200, r.data)
        self.new.refresh_from_db()
        self.assertEqual(self.new.metres_remaining, Decimal("1.50"))
        self.assertEqual(Decimal(str(r.data["metres_remaining"])), Decimal("1.50"))

    def test_more_than_the_roll_has_is_refused(self):
        r = self._write_off(self.old, 1)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("только 0.90 м", r.data["detail"])
        self.old.refresh_from_db()
        self.assertEqual(self.old.remaining_area, Decimal("1.0800"))

    def test_zero_or_missing_metres_is_refused(self):
        r = self._write_off(self.new, 0)
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.post(
            f"/api/warehouse/rolls/{self.new.pk}/write-off/", {"reason_code": "DAMAGE"}, format="json"
        )
        self.assertEqual(r.status_code, 400, r.data)

    def test_general_write_off_in_sqm_is_closed_for_roll_materials(self):
        r = self.client.post(
            "/api/warehouse/materials/write-off/",
            {"material": self.mat.id, "quantity": "2", "reason_code": "DAMAGE"},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("конкретного рулона", r.data["detail"])
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("3.4800"))

    def test_sheet_material_still_uses_the_general_write_off(self):
        sheet = Material.objects.create(
            name="Форекс", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
        )
        receive_lot(
            sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
            sheet_count=Decimal("2"), purchase_cost=Decimal("2000"),
        )
        r = self.client.post(
            "/api/warehouse/materials/write-off/",
            {"material": sheet.id, "quantity": "1", "reason_code": "DEFECT"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        sheet.refresh_from_db()
        self.assertEqual(sheet.quantity, Decimal("4.9536"))

    def test_storekeeper_cannot_write_off(self):
        self.client.force_authenticate(self.store)
        r = self._write_off(self.new, 1)
        self.assertEqual(r.status_code, 403, r.data)
