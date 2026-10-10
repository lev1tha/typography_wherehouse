"""STAFF-05 (часть, волна 2): «ЗП мастера, % от работы» — только 0–100."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase

from accounts.models import User
from services.models import PricingSettings


class MasterPercentTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="mp_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def test_out_of_range_is_400(self):
        for bad in ("-1", "100.01", "250"):
            with self.subTest(bad=bad):
                r = self.client.patch("/api/services/settings/", {"master_commission_percent": bad}, format="json")
                self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(PricingSettings.load().master_commission_percent, Decimal("4"))

    def test_bounds_are_accepted(self):
        for ok in ("0", "100", "6.5"):
            r = self.client.patch("/api/services/settings/", {"master_commission_percent": ok}, format="json")
            self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(PricingSettings.load().master_commission_percent, Decimal("6.5"))

    def test_model_clean(self):
        s = PricingSettings.load()
        s.master_commission_percent = Decimal("150")
        with self.assertRaises(ValidationError):
            s.full_clean()
