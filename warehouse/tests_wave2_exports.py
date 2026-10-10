"""Волна 2, п. 11 (XL-06/STK-09): выгрузки склада в CSV для русского Excel."""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class ExportTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ex_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="ex_store", password="x", role=User.Role.STOREKEEPER)
        self.m = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True, thickness_mm=Decimal("3"),
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1550.50"), piece_price=Decimal("4614"),
        )
        self.lot = receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                               sheet_count=Decimal("10"), purchase_cost=Decimal("32000"), user=self.admin)

    def get(self, url, user, **params):
        self.client.force_authenticate(user)
        r = self.client.get(url, {"export": "csv", **params})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith("﻿".encode()))
        self.assertIn("text/csv", r["Content-Type"])
        return r.content.decode("utf-8-sig").splitlines()

    def test_catalog(self):
        lines = self.get("/api/warehouse/materials/", self.admin)
        self.assertTrue(lines[0].startswith("Название;Тип;"))
        row = lines[1].split(";")
        self.assertEqual(row[0], "Акрил 3мм")
        self.assertIn("29,768", row)          # остаток кв.м с 4 знаками без хвоста
        self.assertIn("10", row)              # в листах
        self.assertIn("1550,50", row)
        self.assertIn("32000,00", row)        # стоимость склада
        store_row = self.get("/api/warehouse/materials/", self.store)[1].split(";")
        self.assertNotIn("32000,00", store_row)

    def test_journal_and_lots(self):
        lines = self.get("/api/warehouse/inventory-logs/", self.admin, material=self.m.id)
        self.assertEqual(len(lines), 2)
        self.assertIn(";Поступление;Акрил 3мм;29,768;кв.м;", lines[1])
        lots = self.get("/api/warehouse/rolls/", self.admin, material=self.m.id)
        self.assertIn(";32000,00;", lots[1])
        self.assertIn("Лист 1.22×2.44 ×10", lots[1])
