"""Волна 2, п. 10 (STK-06): минимальный остаток в листах, «К заказу», Telegram с единицей."""
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, Roll, Supplier
from warehouse.reorder import low_stock_text
from warehouse.rolls import receive_lot


class ReorderTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ro_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="ro_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        self.acr = Material.objects.create(
            name="белый акрил 3 мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("1550"),
        )
        supplier = Supplier.objects.create(name="Глобал")
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "Н-1", "supplier": supplier.id, "received_on": "2026-10-01", "paid_amount": "0",
            "lines": [{"material": self.acr.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                       "sheet_count": "7", "cost": "19376"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.acr.refresh_from_db()

    def test_min_in_sheets_sets_threshold_in_sqm(self):
        r = self.client.patch(f"/api/warehouse/materials/{self.acr.id}/", {"min_stock": "5"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.acr.refresh_from_db()
        self.assertEqual(self.acr.critical_balance, Decimal("14.89"))     # 5 × 2.9768 = 14.884 → вверх
        self.assertEqual(r.data["stock_units"]["stock"], Decimal("7.00"))
        self.assertEqual(r.data["stock_units"]["label"], "лист.")

    def test_reorder_table_and_text(self):
        self.acr.min_stock = Decimal("5")
        self.acr.save()
        r = self.client.post("/api/warehouse/materials/write-off/",
                             {"material": self.acr.id, "sheets": "3", "reason_code": "OTHER"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        rows = self.client.get("/api/warehouse/materials/reorder/").data["results"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["stock"], row["min"], row["to_order"]), (Decimal("4.00"), Decimal("5"), Decimal("6")))
        self.assertEqual(row["supplier"], "Глобал")
        self.assertEqual(row["unit_cost"], Decimal("2768.00"))
        self.acr.refresh_from_db()
        text = low_stock_text(self.acr)
        self.assertIn("осталось 4 лист.", text)
        self.assertIn("Заказать ≈6 лист.", text)
        self.assertIn("Глобал", text)
        self.assertNotIn("11.9072000", text)
        csv = self.client.get("/api/warehouse/materials/reorder/", {"export": "csv"})
        self.assertEqual(csv.status_code, 200)
        body = csv.content.decode("utf-8-sig")
        self.assertIn("белый акрил 3 мм;лист.;4,00;5,00;6,00;Глобал;2768,00;16608,00", body)
        self.client.force_authenticate(self.store)
        rows = self.client.get("/api/warehouse/materials/reorder/").data["results"]
        self.assertIsNone(rows[0]["sum"])

    @override_settings(TELEGRAM_STAFF_BOT_TOKEN="t", TELEGRAM_STAFF_CHAT_IDS=["1"])
    def test_telegram_uses_units(self):
        from integrations import telegram

        self.acr.min_stock = Decimal("5")
        self.acr.save()
        with mock.patch.object(telegram, "_send_message") as sm:
            self.client.post("/api/warehouse/materials/write-off/",
                             {"material": self.acr.id, "sheets": "3", "reason_code": "OTHER"}, format="json")
        self.assertTrue(sm.called)
        self.assertIn("лист.", sm.call_args[0][-1])

    def test_roll_min_in_metres(self):
        film = Material.objects.create(name="Баннер", unit=Material.Unit.SQM, is_roll_material=True,
                                       intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.6"),
                                       price_per_pm=Decimal("300"), min_stock=Decimal("10"))
        self.assertEqual(film.critical_balance, Decimal("16.00"))
        receive_lot(film, form=Roll.Form.ROLL, width=Decimal("1.6"), length=Decimal("8"), purchase_cost=Decimal("2400"))
        film.refresh_from_db()
        rows = {r["name"]: r for r in self.client.get("/api/warehouse/materials/reorder/").data["results"]}
        self.assertEqual(rows["Баннер"]["to_order"], Decimal("12"))
        self.assertEqual(rows["Баннер"]["unit_cost"], Decimal("300.00"))
