"""В журнале склада рулон читается МЕТРАМИ, а не квадратами.

Склад считает площадь, и продажа 5 пог.м баннера шириной 1.6 стояла в ленте
движений как «−8 кв.м». В цехе никто так не меряет: отрезали пять метров.

Пересчитать площадь в метры постфактум нельзя — у каждой партии своя ширина, и
деление на ширину из карточки врало бы (под одной карточкой лежит оракал 1.0,
1.26 и 1.52). Поэтому метры записываются В МОМЕНТ операции.
"""
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from warehouse.models import InventoryLog, Material, MaterialType
from warehouse.rolls import consume_metres, receive_lot, restore_metres, write_off_roll


class JournalMetresTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="jm", password="x", role=User.Role.ADMIN)
        self.mat = Material.objects.create(
            name="Баннер", type=MaterialType.objects.get(code="FILM"),
            unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.6"),
            price_per_pm=Decimal("300"),
        )
        self.roll = receive_lot(
            self.mat, form="ROLL", width=Decimal("1.6"), length=Decimal("50"),
            purchase_cost=Decimal("9750"), code="партия",
        )

    def _last(self):
        return InventoryLog.objects.order_by("-id").first()

    def test_intake_is_logged_in_metres(self):
        entry = InventoryLog.objects.get(type=InventoryLog.Type.SUPPLY)
        self.assertEqual(entry.metres_changed, Decimal("50.000"))
        self.assertEqual(entry.quantity_changed, Decimal("80.0000"))

    def test_sale_is_logged_in_metres(self):
        consume_metres(self.mat, Decimal("5"), user=self.user,
                       log_type=InventoryLog.Type.SALE)
        entry = self._last()
        self.assertEqual(entry.metres_changed, Decimal("-5.000"))
        self.assertEqual(entry.quantity_changed, Decimal("-8.0000"))

    def test_refund_is_logged_in_metres(self):
        consume_metres(self.mat, Decimal("5"), user=self.user, log_type=InventoryLog.Type.SALE)
        restore_metres(self.mat, Decimal("5"), user=self.user,
                       log_type=InventoryLog.Type.RETURN)
        self.assertEqual(self._last().metres_changed, Decimal("5.000"))

    def test_write_off_is_logged_in_metres(self):
        write_off_roll(self.roll, Decimal("2"), reason="брак", user=self.user)
        self.assertEqual(self._last().metres_changed, Decimal("-2.000"))

    def test_sheet_material_keeps_metres_empty(self):
        """У листа мера и есть кв.м — выдумывать метры нечего."""
        sheet = Material.objects.create(
            name="Акрил", type=MaterialType.objects.get(code="ACRYL"),
            unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
        )
        receive_lot(sheet, form="SHEET", width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=10, purchase_cost=Decimal("10000"))
        self.assertIsNone(self._last().metres_changed)
