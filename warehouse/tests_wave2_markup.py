"""Волна 2, п. 6 (STK-03): наценка % у материала, маржа и «ниже закупа».

Партия 1 — 4 614/лист, партия 2 — 6 300/лист, карточка продаёт по 4 900/лист:
каталог должен сказать, что цена ниже закупа последней партии, и подсказать
цену по наценке.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class MarkupTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="mk_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="mk_store", password="x", role=User.Role.STOREKEEPER)
        self.m = Material.objects.create(
            name="акрил 5 мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1650"), piece_price=Decimal("4900"),
        )
        receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("23070"),
                    received_at=timezone.now() - timedelta(days=5))
        receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("31500"))

    def get(self, user):
        self.client.force_authenticate(user)
        r = self.client.get(f"/api/warehouse/materials/{self.m.id}/")
        self.assertEqual(r.status_code, 200)
        return r.data

    def test_below_cost_and_margin(self):
        p = self.get(self.admin)["pricing"]
        self.assertEqual(p["cost"]["sheet"], Decimal("6300.00"))
        self.assertIn("sheet", p["below_cost"])
        self.assertIn("sqm", p["below_cost"])
        self.assertLess(p["margin_percent"], 0)
        self.assertEqual(p["suggested"], {})

    def test_markup_gives_suggested_price(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {"markup_percent": "30"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        p = r.data["pricing"]
        self.assertEqual(p["suggested"]["sheet"], Decimal("8190"))      # 6 300 × 1.3
        self.assertEqual(p["suggested"]["sqm"], Decimal("2752"))        # 2 116.37 × 1.3 = 2 751.28 → вверх

    def test_storekeeper_sees_no_money(self):
        data = self.get(self.store)
        self.assertIsNone(data["pricing"])
        self.assertIsNone(data["markup_percent"])

    def test_piece_material_without_lots_uses_card_price(self):
        bolts = Material.objects.create(name="Болт", unit=Material.Unit.PIECE, purchase_price=Decimal("10"),
                                        price_per_unit=Decimal("15"), markup_percent=Decimal("40"))
        self.client.force_authenticate(self.admin)
        p = self.client.get(f"/api/warehouse/materials/{bolts.id}/").data["pricing"]
        self.assertEqual(p["cost"], {"unit": Decimal("10.00")})
        self.assertEqual(p["suggested"], {"unit": Decimal("14")})
        self.assertEqual(p["margin_percent"], Decimal("33.3"))
        self.assertEqual(p["below_cost"], [])
