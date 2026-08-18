"""Неявный ноль цены/ставки — отказ; явный — подарок (аудит 2026-08-18, п. 8).

У станков в базе `rate_per_pm = 0` («берётся у материала»), у нового материала
ставки резки нет — и вся резка по нему уходила бесплатно, а касса складовщику
строку «Работа» при нулевой ставке даже не показывала. То же с ценой за кв.м /
за лист / за метр: пустой каталог продавал материал за 0. Пустой каталог —
ошибка ввода, а не скидка: сервер называет, чего не хватает и кому это
исправить. Явный override нулём (`cut_rate=0`, `material_price=0`) остаётся
законным подарком админа.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class ImplicitZeroPriceTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="iz_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="iz_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.store)
        # Материал без ставки резки и без цены за кв.м — только цена за лист.
        self.sheet = Material.objects.create(
            name="Новый акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), piece_price=Decimal("3700"),
        )
        receive_lot(
            self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
            sheet_count=Decimal("5"), purchase_cost=Decimal("17500"),
        )
        # Оба станка без своей ставки — как в seed.
        self.cnc = PrintingService.objects.create(
            name="Резка букв", kind=PrintingService.Kind.CUTTING, machine="CNC",
        )
        self.roll = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"),
        )
        receive_lot(self.roll, form=Roll.Form.ROLL, width=Decimal("1.2"), length=Decimal("10"), purchase_cost=Decimal("4000"))
        # Партии подняли остаток в базе — обновляем объекты, иначе `save()` в
        # тестах затёр бы остаток нулём из памяти.
        self.sheet.refresh_from_db()
        self.roll.refresh_from_db()
        self.bolts = Material.objects.create(name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"))

    def _checkout(self, item, **extra):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "pay_full": True, "items": [item], **extra},
            format="json",
        )

    # --- резка ------------------------------------------------------------

    def test_cutting_with_no_rate_anywhere_is_refused(self):
        r = self._checkout({
            "type": "SERVICE", "service": self.cnc.id, "material": self.sheet.id,
            "width": "0.5", "length": "1", "running_meters": "3",
        })
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("Ставка резки не задана", str(r.data))
        self.assertIn("Новый акрил", str(r.data))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_material_rate_makes_the_cut_billable_again(self):
        self.sheet.cut_rate_per_pm = Decimal("120")
        self.sheet.price_per_sqm = Decimal("1500")
        self.sheet.save()
        r = self._checkout({
            "type": "SERVICE", "service": self.cnc.id, "material": self.sheet.id,
            "width": "0.5", "length": "1", "running_meters": "3",
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("360") + Decimal("750"))

    def test_explicit_zero_rate_is_a_gift_admin_may_give(self):
        self.sheet.price_per_sqm = Decimal("1500")
        self.sheet.save()
        self.client.force_authenticate(self.admin)
        r = self._checkout({
            "type": "SERVICE", "service": self.cnc.id, "material": self.sheet.id,
            "width": "0.5", "length": "1", "running_meters": "3", "cut_rate": 0,
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("750"))

    def test_cut_piece_without_sqm_price_is_refused(self):
        self.sheet.cut_rate_per_pm = Decimal("120")
        self.sheet.save()
        r = self._checkout({
            "type": "SERVICE", "service": self.cnc.id, "material": self.sheet.id,
            "width": "0.5", "length": "1", "running_meters": "3",
        })
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("не задана цена за кв.м", str(r.data))

    def test_work_only_line_needs_no_material_price(self):
        """Рез целого листа: материала по площади нет — цена за кв.м не нужна."""
        self.sheet.cut_rate_per_pm = Decimal("120")
        self.sheet.save()
        r = self._checkout({
            "type": "SERVICE", "service": self.cnc.id, "material": self.sheet.id, "running_meters": "3",
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("360"))

    # --- материал ---------------------------------------------------------

    def test_area_sale_without_sqm_price_is_refused(self):
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": "0.5", "mode": "SQM"})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("не задана цена за кв.м", str(r.data))

    def test_piece_sale_uses_the_piece_price(self):
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("3700"))

    def test_roll_without_metre_price_is_refused(self):
        r = self._checkout({"type": "MATERIAL", "material": self.roll.id, "quantity": "1.5", "mode": "METER"})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("не задана цена за пог.м", str(r.data))

    def test_piece_material_without_price_is_refused(self):
        r = self._checkout({"type": "MATERIAL", "material": self.bolts.id, "quantity": 3})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("не задана цена за единицу", str(r.data))

    def test_admin_explicit_zero_price_is_a_gift(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout({"type": "MATERIAL", "material": self.bolts.id, "quantity": 3, "material_price": 0})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("0"))

    def test_zero_quantity_material_line_is_refused(self):
        self.sheet.price_per_sqm = Decimal("1500")
        self.sheet.save()
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": 0, "mode": "PIECE"})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("количество", str(r.data))

    def test_add_items_applies_the_same_rule(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"})
        rid = r.data["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "SERVICE", "service": self.cnc.id, "material": self.sheet.id,
                        "width": "0.5", "length": "1", "running_meters": "3"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("Ставка резки не задана", str(r.data))
