"""Способ продажи материала по площади — явный (аудит 2026-08-18, пункт 2).

`mode` в строке материала не подставлялся: без него сервер считал «кв.м». Так
дозаказ «1 лист» уходил в чек как 1 кв.м по цене за квадрат (1 250 вместо
3 700, со склада 1 кв.м вместо 2.98), а «повторить заказ» с рулоном
превращал 1 пог.м в 1 кв.м по цене за кв.м, которой у рулона нет, — 0 сом.
Резка через дозаказ на рулоне давала строку материала за 0 по той же причине.

Теперь: у листа/рулона режим обязателен; рулон — только METER; METER — только
у рулона; материал по площади к резке по рулону не пристёгивается.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class SaleModeRequiredTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="sm_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Белый акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1250"), piece_price=Decimal("3700"),
        )
        receive_lot(
            self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
            sheet_count=Decimal("5"), purchase_cost=Decimal("17500"),
        )
        self.roll = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"),
            price_per_pm=Decimal("1000"),
        )
        receive_lot(
            self.roll, form=Roll.Form.ROLL, width=Decimal("1.2"), length=Decimal("10"),
            purchase_cost=Decimal("4000"),
        )
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING, rate_per_pm=Decimal("100"),
        )

    def _checkout(self, item):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "pay_full": True, "items": [item]},
            format="json",
        )

    # --- лист ---------------------------------------------------------------

    def test_sheet_material_without_mode_is_refused(self):
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": 1})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("способ продажи", str(r.data))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_sheet_material_with_explicit_mode_sells_as_before(self):
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("3700"))
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": "0.5", "mode": "SQM"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("625"))

    def test_sheet_material_has_no_metres(self):
        r = self._checkout({"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "METER"})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("метрами не продаётся", str(r.data))

    # --- рулон --------------------------------------------------------------

    def test_roll_without_mode_is_refused(self):
        r = self._checkout({"type": "MATERIAL", "material": self.roll.id, "quantity": 2})
        self.assertEqual(r.status_code, 400, r.data)

    def test_roll_by_area_or_piece_is_refused_not_sold_for_free(self):
        for mode in ("SQM", "PIECE"):
            r = self._checkout({"type": "MATERIAL", "material": self.roll.id, "quantity": 1, "mode": mode})
            self.assertEqual(r.status_code, 400, (mode, r.data))
            self.assertIn("погонными метрами", str(r.data))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_roll_by_metre_still_works(self):
        r = self._checkout({"type": "MATERIAL", "material": self.roll.id, "quantity": "1.5", "mode": "METER"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("1500"))
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.quantity, Decimal("12") - Decimal("1.8"))

    def test_cutting_on_a_roll_does_not_add_material_by_area(self):
        # Резка с размерами куска по рулону — материал по площади ушёл бы за 0.
        r = self._checkout({
            "type": "SERVICE", "service": self.cutting.id, "material": self.roll.id,
            "width": "0.5", "length": "1", "running_meters": "3",
        })
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("отдельной строкой", str(r.data))
        # А работа реза по рулону без размеров куска — обычная строка работы.
        r = self._checkout({
            "type": "SERVICE", "service": self.cutting.id, "material": self.roll.id,
            "running_meters": "3",
        })
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("300"))
        self.assertEqual(len(r.data["items"]), 1)

    # --- штучный материал ---------------------------------------------------

    def test_piece_material_needs_no_mode(self):
        r = self._checkout({"type": "MATERIAL", "material": self.bolts.id, "quantity": 3})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("30"))

    def test_piece_material_has_no_metres_either(self):
        r = self._checkout({"type": "MATERIAL", "material": self.bolts.id, "quantity": 3, "mode": "METER"})
        self.assertEqual(r.status_code, 400, r.data)

    # --- дозаказ идёт через тот же сериализатор ------------------------------

    def test_add_items_applies_the_same_rule(self):
        r = self._checkout({"type": "MATERIAL", "material": self.bolts.id, "quantity": 1})
        rid = r.data["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.roll.id, "quantity": 2, "mode": "SQM"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("3710"))
