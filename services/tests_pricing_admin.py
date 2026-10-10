"""«Цены и услуги»: журнал «было → стало», неизвестные настройки, таблица
коэффициентов толщины и матрица ставок (2026-10-10, XL-07/F3, CALC-02, -05).
"""
from decimal import Decimal as D

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from services.models import PricingSettings, PrintingService, RateMatrixEntry, ServiceRecipe, ThicknessCoefficient
from warehouse.models import Material

SETTINGS = "/api/services/settings/"
SERVICES = "/api/services/services/"


class Base(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="pa_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="pa_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        self.cut = PrintingService.objects.create(
            name="Резка лазер 2", kind="CUTTING", machine="LASER", rate_per_pm=D("30"),
        )
        self.engr = PrintingService.objects.create(name="Гравировка 2", kind="ENGRAVING", rate_flat=D("3000"))
        self.mat = Material.objects.create(
            name="Форекс 5", unit=Material.Unit.SQM, is_roll_material=True, thickness_mm=D("5"),
            sheet_width=D("1"), sheet_height=D("2"), price_per_sqm=D("100"), cut_rate_per_pm=D("40"),
        )

    def last_logs(self, n=10):
        return list(AuditLog.objects.order_by("-id").values_list("action", flat=True)[:n])


class SettingsTests(Base):
    def test_unknown_setting_is_a_400_and_nothing_is_saved(self):
        out = self.client.patch(
            SETTINGS, {"engraving_passes": "3", "thickness_coef": "1.35", "min_line_amount": "700"},
            format="json",
        )
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn("engraving_passes", out.data)
        self.assertIn("thickness_coef", out.data)
        self.assertEqual(PricingSettings.load().min_line_amount, D("0"))

    def test_known_settings_are_saved_and_returned(self):
        out = self.client.patch(
            SETTINGS,
            {"min_mode": "ORDER", "rounding_mode": "ORDER", "confirm_line_total": "50000",
             "staff_line_cap": "20000", "staff_min_price_percent": "60", "debt_warn_days": 45},
            format="json",
        )
        self.assertEqual(out.status_code, 200, out.data)
        s = PricingSettings.load()
        self.assertEqual((s.min_mode, s.rounding_mode, s.debt_warn_days), ("ORDER", "ORDER", 45))
        self.assertEqual((s.confirm_line_total, s.staff_line_cap), (D("50000"), D("20000")))

    def test_defaults_keep_the_prod_behaviour(self):
        s = PricingSettings.load()
        self.assertEqual((s.min_mode, s.rounding_mode), ("PART", "LINE"))
        self.assertEqual((s.min_line_amount, s.urgency_percent), (D("0"), D("0")))
        self.assertEqual((s.confirm_line_total, s.staff_line_cap), (D("100000"), D("0")))
        self.assertEqual((s.staff_min_price_percent, s.debt_warn_days), (D("0"), 0))

    def test_bad_values_are_refused(self):
        for body in ({"min_mode": "WEEKLY"}, {"rounding_mode": "x"}, {"staff_min_price_percent": "150"},
                     {"confirm_line_total": "-1"}, {"debt_warn_days": -3}):
            self.assertEqual(self.client.patch(SETTINGS, body, format="json").status_code, 400, body)

    def test_settings_changes_are_journaled_as_was_became(self):
        self.client.patch(SETTINGS, {"min_line_amount": "500", "min_mode": "WORK", "urgency_percent": "25"},
                          format="json")
        logs = "\n".join(self.last_logs())
        self.assertIn("минимум строки услуги 0 → 500", logs)
        self.assertIn("наценка за срочность, % 0 → 25", logs)
        self.assertIn("к чему применять минимум PART → WORK", logs)

    def test_unchanged_settings_write_nothing(self):
        before = AuditLog.objects.count()
        self.client.patch(SETTINGS, {"min_line_amount": "0"}, format="json")
        self.assertEqual(AuditLog.objects.count(), before)

    def test_only_admin_edits_but_everyone_reads_the_public_rules(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.patch(SETTINGS, {"min_line_amount": "1"}, format="json").status_code, 403)
        rules = self.client.get("/api/services/rules/").data
        for key in ("min_mode", "rounding_mode", "confirm_line_total", "staff_line_cap",
                    "staff_min_price_percent", "debt_warn_days", "online_payments_enabled"):
            self.assertIn(key, rules)
        self.assertNotIn("master_commission_percent", rules)


class ServiceJournalTests(Base):
    def test_every_price_and_rate_change_is_journaled(self):
        out = self.client.patch(
            f"{SERVICES}{self.engr.id}/",
            {"rate_flat": "3500", "rate_per_pm": "10", "rate_per_piece": "20", "base_price": "99",
             "min_line_amount": "400", "negotiable_price": True},
            format="json",
        )
        self.assertEqual(out.status_code, 200, out.data)
        logs = "\n".join(self.last_logs())
        for expected in ("ставка за кв.м 3000 → 3500", "ставка за пог.м 0 → 10", "ставка за букву 0 → 20",
                         "базовая цена 0 → 99", "минимум строки (пусто — общий) — → 400",
                         "цена по договорённости нет → да"):
            self.assertIn(expected, logs)
        self.assertIn("Услуга «Гравировка 2»", logs)

    def test_unchanged_service_patch_writes_nothing(self):
        before = AuditLog.objects.count()
        self.client.patch(f"{SERVICES}{self.engr.id}/", {"rate_flat": "3000"}, format="json")
        self.assertEqual(AuditLog.objects.count(), before)

    def test_create_and_delete_are_journaled(self):
        out = self.client.post(SERVICES, {"name": "Сверление", "kind": "OTHER", "base_price": "150"},
                               format="json")
        self.assertEqual(out.status_code, 201, out.data)
        self.assertIn("Добавлена услуга «Сверление»", self.last_logs(1)[0])
        self.client.delete(f"{SERVICES}{out.data['id']}/")
        self.assertIn("Удалена услуга «Сверление»", self.last_logs(1)[0])

    def test_recipe_changes_are_journaled(self):
        glue = Material.objects.create(name="Клей 2", unit=Material.Unit.PIECE, quantity=D("5"),
                                       price_per_unit=D("10"), purchase_price=D("4"))
        out = self.client.post("/api/services/recipes/", {
            "service": self.cut.id, "material": glue.id, "consumption_per_unit": "0.01",
            "consumption_mode": "PER_PM",
        }, format="json")
        self.assertEqual(out.status_code, 201, out.data)
        self.assertIn("Добавлена техкарта", self.last_logs(1)[0])
        self.client.patch(f"/api/services/recipes/{out.data['id']}/", {"consumption_per_unit": "0.02"}, format="json")
        self.assertIn("Изменена техкарта", self.last_logs(1)[0])

    def test_per_metre_mode_is_available_in_the_recipe(self):
        self.assertIn("PER_PM", [v for v, _ in ServiceRecipe.Mode.choices])


class ThicknessTableTests(Base):
    URL = "/api/services/thickness-coefficients/"

    def test_admin_fills_the_table_and_everyone_reads_it(self):
        out = self.client.post(self.URL, {"kind": "CUTTING", "thickness_from": "5", "coefficient": "1.35"},
                               format="json")
        self.assertEqual(out.status_code, 201, out.data)
        self.assertIn("Коэффициент толщины", self.last_logs(1)[0])
        self.assertIn("добавлен × 1.35", self.last_logs(1)[0])
        self.client.force_authenticate(self.store)
        self.assertEqual(len(self.client.get(self.URL).data["results"] if "results" in self.client.get(self.URL).data
                             else self.client.get(self.URL).data), 1)
        self.assertEqual(self.client.post(self.URL, {"kind": "CUTTING", "thickness_from": "8",
                                                      "coefficient": "2"}, format="json").status_code, 403)

    def test_update_and_delete_are_journaled(self):
        row = ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("5"), coefficient=D("1.35"))
        self.client.patch(f"{self.URL}{row.id}/", {"coefficient": "1.4"}, format="json")
        self.assertIn("× 1.35 → × 1.4", self.last_logs(1)[0])
        self.client.delete(f"{self.URL}{row.id}/")
        self.assertIn("убран × 1.4", self.last_logs(1)[0])

    def test_only_area_kinds_and_unique_rows(self):
        bad = self.client.post(self.URL, {"kind": "OTHER", "thickness_from": "5", "coefficient": "1.2"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.client.post(self.URL, {"kind": "CUTTING", "thickness_from": "5", "coefficient": "1.2"}, format="json")
        twice = self.client.post(self.URL, {"kind": "CUTTING", "thickness_from": "5", "coefficient": "1.3"}, format="json")
        self.assertEqual(twice.status_code, 400)

    def test_zero_or_huge_coefficients_are_refused(self):
        for value in ("0", "-1", "101"):
            out = self.client.post(self.URL, {"kind": "CUTTING", "thickness_from": "5", "coefficient": value},
                                   format="json")
            self.assertEqual(out.status_code, 400, value)


class RateMatrixApiTests(Base):
    URL = "/api/services/rate-matrix/"

    def test_row_by_material_and_by_thickness(self):
        a = self.client.post(self.URL, {"service": self.cut.id, "material": self.mat.id, "rate": "28"}, format="json")
        b = self.client.post(self.URL, {"service": self.cut.id, "thickness_from": "6", "rate": "33"}, format="json")
        self.assertEqual((a.status_code, b.status_code), (201, 201), (a.data, b.data))
        self.assertEqual(a.data["material_name"], "Форекс 5")
        shown = self.client.get(f"{SERVICES}{self.cut.id}/").data["rate_matrix"]
        self.assertEqual(len(shown), 2)
        self.assertIn("Матрица ставок", self.last_logs(1)[0])

    def test_exactly_one_key(self):
        both = self.client.post(self.URL, {"service": self.cut.id, "material": self.mat.id,
                                           "thickness_from": "5", "rate": "1"}, format="json")
        neither = self.client.post(self.URL, {"service": self.cut.id, "rate": "1"}, format="json")
        self.assertEqual((both.status_code, neither.status_code), (400, 400))

    def test_only_area_services_have_a_matrix(self):
        fixed = PrintingService.objects.create(name="Монтаж 2", kind="OTHER", base_price=D("100"))
        out = self.client.post(self.URL, {"service": fixed.id, "material": self.mat.id, "rate": "1"}, format="json")
        self.assertEqual(out.status_code, 400, out.data)

    def test_duplicates_are_refused(self):
        body = {"service": self.cut.id, "material": self.mat.id, "rate": "28"}
        self.client.post(self.URL, body, format="json")
        self.assertEqual(self.client.post(self.URL, body, format="json").status_code, 400)

    def test_rate_change_and_removal_are_journaled(self):
        row = RateMatrixEntry.objects.create(service=self.cut, material=self.mat, rate=D("28"))
        self.client.patch(f"{self.URL}{row.id}/", {"rate": "31"}, format="json")
        self.assertIn("ставка 28 → 31", self.last_logs(1)[0])
        self.client.delete(f"{self.URL}{row.id}/")
        self.assertIn("убрана ставка 31", self.last_logs(1)[0])

    def test_storekeeper_reads_but_does_not_write(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.get(self.URL).status_code, 200)
        self.assertEqual(self.client.post(self.URL, {"service": self.cut.id, "material": self.mat.id,
                                                     "rate": "1"}, format="json").status_code, 403)


class ResolvedRateApiTests(Base):
    URL = "/api/services/rate/"

    def test_chain_and_coefficient(self):
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("5"), coefficient=D("1.5"))
        out = self.client.get(self.URL, {"service": self.cut.id, "material": self.mat.id})
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["base"])), D("30"))
        self.assertEqual(D(str(out.data["rate"])), D("45"))
        self.assertEqual((out.data["source"], D(str(out.data["coefficient"]))), ("machine", D("1.5")))

    def test_matrix_wins(self):
        RateMatrixEntry.objects.create(service=self.cut, material=self.mat, rate=D("28"))
        out = self.client.get(self.URL, {"service": self.cut.id, "material": self.mat.id})
        self.assertEqual((D(str(out.data["rate"])), out.data["source"]), (D("28"), "matrix_material"))

    def test_missing_service_is_404(self):
        self.assertEqual(self.client.get(self.URL, {"service": 99999}).status_code, 404)
