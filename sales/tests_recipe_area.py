"""Техкарта «на кв.м» считается от ПЛОЩАДИ куска, а не от длины реза
(аудит 2026-08-18, п. 16).

У строки резки `quantity` — погонные метры реза; норма «на кв.м» умножалась на
неё: 0.1 клея на кв.м при куске 0.5 кв.м и 8 пог.м реза списывала 0.8 вместо
0.05 — в 16 раз больше. Техкарты в интерфейсе только читаются (заводятся через
админку), поэтому дыра спала; теперь и списание, и «расход по техкартам» в
обзоре считают от площади.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from services.models import PrintingService, ServiceRecipe
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class RecipeAreaTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ra_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"), cut_rate_per_pm=Decimal("120"),
        )
        receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("17500"))
        self.glue = Material.objects.create(
            name="Клей", unit=Material.Unit.LITER, quantity=Decimal("20"), purchase_price=Decimal("400"),
        )
        self.tape = Material.objects.create(
            name="Скотч", unit=Material.Unit.PIECE, quantity=Decimal("50"), purchase_price=Decimal("30"),
        )
        self.cutting = PrintingService.objects.create(name="Резка", kind=PrintingService.Kind.CUTTING)
        ServiceRecipe.objects.create(
            service=self.cutting, material=self.glue, consumption_per_unit=Decimal("0.1"),
            consumption_mode=ServiceRecipe.Mode.PER_SQM,
        )
        ServiceRecipe.objects.create(
            service=self.cutting, material=self.tape, consumption_per_unit=Decimal("1"),
            consumption_mode=ServiceRecipe.Mode.FIXED,
        )

    def test_per_sqm_recipe_uses_the_piece_area(self):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "SERVICE", "service": self.cutting.id, "material": self.sheet.id,
                       "width": "0.5", "length": "1", "running_meters": "8"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.glue.refresh_from_db()
        self.tape.refresh_from_db()
        self.assertEqual(self.glue.quantity, Decimal("20") - Decimal("0.05"))   # 0.1 × 0.5 кв.м, не × 8 м
        self.assertEqual(self.tape.quantity, Decimal("49"))                     # фикс — раз на строку
        # Себестоимость строки работы — клей 0.05 × 400 + скотч 30.
        work = [i for i in r.data["items"] if i["type"] == "SERVICE"][0]
        self.assertEqual(Decimal(str(work["cost_total"])), Decimal("50.00"))
        # Обзор считает по той же формуле.
        d = self.client.get("/api/audit/dashboard/").data
        self.assertEqual(Decimal(str(d["materials_consumed_by_services"])), Decimal("1.05"))

    def test_whole_sheet_cut_has_no_area_for_per_sqm_recipes(self):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "SERVICE", "service": self.cutting.id, "material": self.sheet.id,
                       "running_meters": "3"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.glue.refresh_from_db()
        self.tape.refresh_from_db()
        self.assertEqual(self.glue.quantity, Decimal("20"))   # площади куска нет — нечего умножать
        self.assertEqual(self.tape.quantity, Decimal("49"))
