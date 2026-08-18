"""Остаток рулонного материала правится ТОЛЬКО промером по рулону.

Общая инвентаризация в кв.м (`/materials/adjust/`) для материала, который
продаётся метрами, отвечает не на тот вопрос: рулон меряют рулеткой, по одному
и в метрах. Хуже того — расхождение она гнала через FIFO: «промерил рулон №23,
там 10.8 вместо 12.3» превращалось в списание полутора метров со СТАРЕЙШЕГО
рулона. Остаток показывался в метрах, а сверялся в квадратах.

Единственное, что у такого материала правится числом напрямую, — «хвост сверх
партий»: кв.м, которые не принадлежат ни одному рулону (материал переключили на
рулон уже с остатком, старая правка остатка подняла число без партии). Их не
списать ни продажей, ни промером — для них отдельное действие «свести с
рулонами», которое приводит число к сумме партий и пишет разницу в журнал.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from warehouse.models import InventoryLog, Material, Roll


class AdjustRollGuardTests(APITestCase):
    ADJUST = "/api/warehouse/materials/adjust/"
    RECONCILE = "/api/warehouse/materials/reconcile-lots/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ag_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("1.0"), price_per_pm=Decimal("300"),
        )
        # Два рулона: старый 12.3 м и новый 20 м — как на полке.
        for code, length in (("Р-1", "12.3"), ("Р-2", "20")):
            r = self.client.post("/api/warehouse/materials/receive-roll/", {
                "material": self.mat.id, "form": "ROLL", "width": "1.0",
                "length": length, "cost_per_pm": "100", "code": code,
            }, format="json")
            self.assertEqual(r.status_code, 201, r.data)
        self.old_roll = Roll.objects.get(code="Р-1")
        self.new_roll = Roll.objects.get(code="Р-2")
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("32.3"))

    def _lots(self):
        return sum((r.remaining_area for r in Roll.objects.filter(material=self.mat)), Decimal("0"))

    # --- adjust закрыт для рулонных ---------------------------------------------

    def test_adjust_refuses_metre_material_and_touches_nothing(self):
        """Владелец промерил №23 и ввёл общий остаток — раньше полтора метра
        уходили со старейшего рулона. Теперь 400 и отсылка к промеру."""
        res = self.client.post(self.ADJUST, {
            "material": self.mat.id, "counted_quantity": "30.8",
        }, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("Промер", res.data["detail"])
        self.assertNotIn("Свести", res.data["detail"])
        self.mat.refresh_from_db()
        self.old_roll.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("32.3"))
        self.assertEqual(self.old_roll.remaining_area, Decimal("12.3"))
        self.assertFalse(
            InventoryLog.objects.filter(material=self.mat, type=InventoryLog.Type.ADJUSTMENT).exists()
        )

    def test_adjust_names_the_tail_when_stock_and_lots_disagree(self):
        """Расхождение с суммой партий названо цифрами и указано, чем сводить."""
        Material.objects.filter(pk=self.mat.pk).update(quantity=Decimal("35.3"))
        res = self.client.post(self.ADJUST, {
            "material": self.mat.id, "counted_quantity": "32.3",
        }, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("35.3", res.data["detail"])
        self.assertIn("32.3", res.data["detail"])
        self.assertIn("Свести с рулонами", res.data["detail"])
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("35.3"))

    def test_sheet_material_adjust_still_works(self):
        """Листовой материал инвентаризацией правится как раньше."""
        sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
        )
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": sheet.id, "form": "SHEET", "width": "1.22",
            "height": "2.44", "sheet_count": "3", "purchase_cost": "7800",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        res = self.client.post(self.ADJUST, {
            "material": sheet.id, "counted_quantity": "5.95",
        }, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        sheet.refresh_from_db()
        self.assertEqual(sheet.quantity, Decimal("5.95"))

    # --- сведение хвоста ---------------------------------------------------------

    def test_reconcile_brings_stock_to_the_sum_of_lots_and_logs_the_delta(self):
        Material.objects.filter(pk=self.mat.pk).update(quantity=Decimal("35.3"))
        res = self.client.post(self.RECONCILE, {"material": self.mat.id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, self._lots())
        self.assertEqual(self.mat.quantity, Decimal("32.3"))
        # Рулоны не тронуты — хвост сняли с числа, а не с полки.
        self.old_roll.refresh_from_db()
        self.new_roll.refresh_from_db()
        self.assertEqual(self.old_roll.remaining_area, Decimal("12.3"))
        self.assertEqual(self.new_roll.remaining_area, Decimal("20"))
        entry = InventoryLog.objects.get(material=self.mat, type=InventoryLog.Type.ADJUSTMENT)
        self.assertEqual(entry.quantity_changed, Decimal("-3"))
        self.assertIn("Сведение остатка с рулонами", entry.reason)
        self.assertTrue(AuditLog.objects.filter(action__contains="Сведение остатка").exists())

    def test_reconcile_works_upwards_too(self):
        """Число ниже суммы партий — тоже расхождение, сводится в ту же сторону."""
        Material.objects.filter(pk=self.mat.pk).update(quantity=Decimal("30"))
        res = self.client.post(self.RECONCILE, {"material": self.mat.id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("32.3"))
        entry = InventoryLog.objects.get(material=self.mat, type=InventoryLog.Type.ADJUSTMENT)
        self.assertEqual(entry.quantity_changed, Decimal("2.3"))

    def test_reconcile_refuses_when_nothing_to_reconcile(self):
        res = self.client.post(self.RECONCILE, {"material": self.mat.id}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("сводить нечего", res.data["detail"])
        self.assertFalse(InventoryLog.objects.filter(type=InventoryLog.Type.ADJUSTMENT).exists())

    def test_reconcile_is_only_for_metre_materials(self):
        """У листа хвост сверх партий законен и списывается продажей — сводить
        его нечем и незачем."""
        sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET, quantity=Decimal("5"),
        )
        res = self.client.post(self.RECONCILE, {"material": sheet.id}, format="json")
        self.assertEqual(res.status_code, 400, res.data)
        sheet.refresh_from_db()
        self.assertEqual(sheet.quantity, Decimal("5"))

    def test_reconcile_is_admin_only(self):
        keeper = User.objects.create_user(
            username="ag_keeper", password="x", role=User.Role.STOREKEEPER
        )
        Material.objects.filter(pk=self.mat.pk).update(quantity=Decimal("35.3"))
        self.client.force_authenticate(keeper)
        res = self.client.post(self.RECONCILE, {"material": self.mat.id}, format="json")
        self.assertEqual(res.status_code, 403)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("35.3"))
