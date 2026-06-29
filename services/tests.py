"""E2E API tests for service pricing.

Covers the reported bug: an admin changes a service rate and the storekeeper
(who only reads) must see the new rate. The regression was a frontend one
(editing the wrong field), but these tests pin the API contract: every billing
rate is writable by admin, persists, and is returned to the storekeeper.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from services.models import PrintingService


class ServicePricingAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_t", password="x", role=User.Role.ADMIN
        )
        self.store = User.objects.create_user(
            username="store_t", password="x", role=User.Role.STOREKEEPER
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            rate_flat=Decimal("200"),
        )
        self.exterior = PrintingService.objects.create(
            name="Наружная установка", kind=PrintingService.Kind.INSTALL_EXTERIOR,
            rate_per_piece=Decimal("150"),
        )
        self.interior = PrintingService.objects.create(
            name="Внутренняя установка", kind=PrintingService.Kind.INSTALL_INTERIOR,
            rate_flat=Decimal("100"),
        )

    def _store_sees(self, service_id, field):
        """What the storekeeper's POS fetches for a service field."""
        self.client.force_authenticate(self.store)
        r = self.client.get(f"/api/services/services/{service_id}/")
        self.assertEqual(r.status_code, 200)
        return r.data[field]

    def test_admin_changes_cutting_rate_storekeeper_sees_it(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(
            f"/api/services/services/{self.cutting.id}/",
            {"rate_flat": "250"}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.cutting.refresh_from_db()
        self.assertEqual(self.cutting.rate_flat, Decimal("250"))
        self.assertEqual(Decimal(self._store_sees(self.cutting.id, "rate_flat")), Decimal("250"))

    def test_admin_changes_exterior_rate_per_piece_reflected(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(
            f"/api/services/services/{self.exterior.id}/",
            {"rate_per_piece": "300"}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.exterior.refresh_from_db()
        self.assertEqual(self.exterior.rate_per_piece, Decimal("300"))
        # The field that actually drives exterior-install billing in the POS.
        self.assertEqual(Decimal(self._store_sees(self.exterior.id, "rate_per_piece")), Decimal("300"))

    def test_admin_changes_interior_rate_flat_reflected(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(
            f"/api/services/services/{self.interior.id}/",
            {"rate_flat": "175"}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Decimal(self._store_sees(self.interior.id, "rate_flat")), Decimal("175"))

    def test_storekeeper_cannot_edit_rates(self):
        self.client.force_authenticate(self.store)
        r = self.client.patch(
            f"/api/services/services/{self.cutting.id}/",
            {"rate_flat": "999"}, format="json",
        )
        self.assertEqual(r.status_code, 403)
        self.cutting.refresh_from_db()
        self.assertEqual(self.cutting.rate_flat, Decimal("200"))

    def test_uses_flags_match_billing_fields(self):
        """The frontend builds its editable fields off these flags — pin them."""
        self.client.force_authenticate(self.store)
        by_id = {s["id"]: s for s in self.client.get("/api/services/services/").data["results"]}
        self.assertTrue(by_id[self.cutting.id]["uses_area"])
        self.assertTrue(by_id[self.cutting.id]["uses_material"])
        self.assertTrue(by_id[self.cutting.id]["uses_running_meter"])
        self.assertFalse(by_id[self.interior.id]["uses_running_meter"])
        self.assertTrue(by_id[self.exterior.id]["uses_pieces"])
        self.assertTrue(by_id[self.interior.id]["uses_area"])
        self.assertTrue(by_id[self.interior.id]["uses_material"])
        self.assertFalse(by_id[self.exterior.id]["uses_area"])

    def test_master_commission_settings_admin_only(self):
        # Storekeeper cannot read/update settings.
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.get("/api/services/settings/").status_code, 403)
        # Admin reads default and updates it.
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/services/settings/")
        self.assertEqual(r.status_code, 200)
        r = self.client.patch(
            "/api/services/settings/", {"master_commission_percent": "5"}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Decimal(r.data["master_commission_percent"]), Decimal("5"))
