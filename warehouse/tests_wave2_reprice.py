"""Волна 2, п. 7 (XL-05/CALC-09/XL-07): переоценка × %, пачка-обновление, журнал цен."""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from warehouse.models import Material, MaterialType


class RepriceTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="rp_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="rp_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        self.acr = MaterialType.objects.create(code="acr-t", name="Акрил-т")
        self.a3 = Material.objects.create(name="Акрил 3", type=self.acr, thickness_mm=Decimal("3"),
                                          unit=Material.Unit.SQM, is_roll_material=True,
                                          sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
                                          price_per_sqm=Decimal("1550"), piece_price=Decimal("4614"))
        self.a5 = Material.objects.create(name="Акрил 5", type=self.acr, thickness_mm=Decimal("5"),
                                          unit=Material.Unit.SQM, is_roll_material=True,
                                          sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
                                          price_per_sqm=Decimal("2000"), piece_price=Decimal("0"))
        self.bolt = Material.objects.create(name="Болт", unit=Material.Unit.PIECE, price_per_unit=Decimal("15"))

    def test_preview_then_apply_by_type(self):
        body = {"percent": "10", "type": self.acr.id, "step": "10"}
        r = self.client.post("/api/warehouse/materials/reprice/", body, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        rows = {row["name"]: row for row in r.data["rows"]}
        self.assertEqual(set(rows), {"Акрил 3", "Акрил 5"})
        a3 = {c["field"]: (c["before"], c["after"]) for c in rows["Акрил 3"]["changes"]}
        self.assertEqual(a3["price_per_sqm"], (Decimal("1550.00"), Decimal("1710.00")))   # 1705 → 1710
        self.assertEqual(a3["piece_price"], (Decimal("4614.00"), Decimal("5080.00")))     # 5075.4 → 5080
        # Ноль у цены листа значит «листом не продаётся» — не оживляем.
        self.assertNotIn("piece_price", {c["field"] for c in rows["Акрил 5"]["changes"]})
        self.a3.refresh_from_db()
        self.assertEqual(self.a3.price_per_sqm, Decimal("1550"))                           # предпросмотр не пишет
        r = self.client.post("/api/warehouse/materials/reprice/", dict(body, apply=True), format="json")
        self.assertEqual(r.data["applied"], 2)
        self.a3.refresh_from_db()
        self.bolt.refresh_from_db()
        self.assertEqual(self.a3.price_per_sqm, Decimal("1710"))
        self.assertEqual(self.bolt.price_per_unit, Decimal("15"))
        log = AuditLog.objects.filter(kind="price", action__contains="Акрил 3").latest("id")
        self.assertIn("1 550 → 1 710", log.action)
        self.assertIn("переоценка +10 %", log.action)

    def test_selected_ids_and_thickness(self):
        r = self.client.post("/api/warehouse/materials/reprice/",
                             {"percent": "-5", "ids": [self.a3.id, self.bolt.id], "thickness_mm": "3,0"}, format="json")
        self.assertEqual([row["name"] for row in r.data["rows"]], ["Акрил 3"])

    def test_bad_percent_and_storekeeper(self):
        self.assertEqual(self.client.post("/api/warehouse/materials/reprice/", {"percent": "0"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/warehouse/materials/reprice/", {"percent": "abc"}, format="json").status_code, 400)
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.post("/api/warehouse/materials/reprice/", {"percent": "10"}, format="json").status_code, 403)

    def test_card_edit_writes_before_after(self):
        r = self.client.patch(f"/api/warehouse/materials/{self.a3.id}/",
                              {"price_per_sqm": "1705", "cut_rate_per_pm": "72"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        log = AuditLog.objects.filter(kind="price").latest("id")
        self.assertIn("цена за кв.м 1 550 → 1 705", log.action)
        self.assertIn("ставка резки за пог.м 0 → 72", log.action)
        n = AuditLog.objects.count()
        self.client.patch(f"/api/warehouse/materials/{self.a3.id}/", {"color": "белый"}, format="json")
        self.assertEqual(AuditLog.objects.count(), n)          # не цена — не пишем


class BulkUpsertTests(APITestCase):
    URL = "/api/warehouse/materials/bulk/"

    def setUp(self):
        self.admin = User.objects.create_user(username="bu_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(name="форекс 3мм", unit=Material.Unit.SQM, is_roll_material=True,
                                             sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
                                             price_per_sqm=Decimal("500"), piece_price=Decimal("1488"))

    def test_create_mode_still_refuses_existing_name(self):
        r = self.client.post(self.URL, {"rows": [{"name": "Форекс 3мм", "price_per_sqm": "550"}]}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_upsert_preview_and_apply(self):
        rows = [
            {"name": "Форекс 3мм", "price_per_sqm": "550", "price_per_unit": "1 637"},
            {"name": "Новый лист", "sheet_width": "1,22", "sheet_height": "2,44", "piece_price": "2000"},
        ]
        r = self.client.post(self.URL, {"rows": rows, "mode": "upsert", "preview": True}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["create"], ["Новый лист"])
        changes = {c["field"]: (c["before"], c["after"]) for c in r.data["update"][0]["changes"]}
        # «за лист/шт» без размера листа в строке — у листового это цена листа.
        self.assertEqual(changes, {"price_per_sqm": (Decimal("500.00"), Decimal("550")),
                                   "piece_price": (Decimal("1488.00"), Decimal("1637"))})
        self.assertFalse(Material.objects.filter(name="Новый лист").exists())
        r = self.client.post(self.URL, {"rows": rows, "mode": "upsert"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data["created"], r.data["updated"]), (1, 1))
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.price_per_sqm, Decimal("550"))
        self.assertEqual(self.sheet.piece_price, Decimal("1637"))
        self.assertEqual(Material.objects.count(), 2)
        self.assertTrue(AuditLog.objects.filter(kind="price", action__contains="обновление пачкой").exists())
