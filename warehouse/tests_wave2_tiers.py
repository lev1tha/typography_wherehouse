"""Волна 2, п. 14 (CLI-02, часть): ступени опта у материала.

«От 10 листов — 3 500, от 25 — 3 200»: 25 листов = 80 000, а не 85 000 по
одной ступени.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class TierTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ti_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.m = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("1250"),
            piece_price=Decimal("3700"), wholesale_price=Decimal("3400"), wholesale_min_qty=Decimal("10"),
        )
        receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("40"), purchase_cost=Decimal("40000"))

    def test_tiers_via_api_and_in_checkout(self):
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {
            "price_tiers": [{"min_qty": "25", "price": "3200"}, {"min_qty": "5", "price": "3600"}],
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([t["min_qty"] for t in r.data["price_tiers"]], ["5.00", "25.00"])
        self.assertIn("ступени опта", AuditLog.objects.filter(kind="price").latest("id").action)
        self.m.refresh_from_db()
        self.assertEqual(self.m.piece_price_for_qty(1), Decimal("3700"))
        self.assertEqual(self.m.piece_price_for_qty(5), Decimal("3600"))
        self.assertEqual(self.m.piece_price_for_qty(10), Decimal("3400"))    # прежняя пара — тоже ступень
        self.assertEqual(self.m.piece_price_for_qty(25), Decimal("3200"))
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "confirmed_warnings": ["line_total_high"],
            "items": [{"type": "MATERIAL", "material": self.m.id, "mode": "PIECE", "quantity": "25"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("80000"))

    def test_duplicate_threshold_is_400_and_empty_list_clears(self):
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {
            "price_tiers": [{"min_qty": "5", "price": "3600"}, {"min_qty": "5", "price": "3500"}],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {
            "price_tiers": [{"min_qty": "5", "price": "3600"}]}, format="json")
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {"price_tiers": []}, format="json")
        self.assertEqual(r.data["price_tiers"], [])
