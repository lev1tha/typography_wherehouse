"""Волна 2, п. 2 (XL-01): вставка из русского Excel в «Ввести пачкой».

«1,22», «4,5», «2 679» с обычным и неразрывным пробелом, «2679,50 сом» —
пачка сохраняется одним запросом, числа приходят как надо.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material
from warehouse.numbers import normalize_number_text


class NormalizeTests(APITestCase):
    def test_variants(self):
        cases = {
            "1,22": "1.22", "2 679": "2679", "2 679": "2679", "2 679,50": "2679.50",
            "2679 сом": "2679", "2 679,50 сом.": "2679.50", "1.234,56": "1234.56",
            "1,234.56": "1234.56", "  4,5 ": "4.5", "": "", "abc": "abc", "1,2,3": "1,2,3",
        }
        for raw, want in cases.items():
            self.assertEqual(normalize_number_text(raw), want, raw)
        self.assertEqual(normalize_number_text(5), 5)


class BulkPasteTests(APITestCase):
    URL = "/api/warehouse/materials/bulk/"

    def setUp(self):
        self.admin = User.objects.create_user(username="bp_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def row(self, **kw):
        base = {"type": "", "thickness_mm": "4,5", "sheet_width": "1,22", "sheet_height": "2,44",
                "price_per_sqm": "900", "cut_rate_per_pm": "65", "piece_price": "2 679"}
        base.update(kw)
        return base

    def test_russian_excel_numbers_save_in_one_request(self):
        rows = [
            self.row(name="К1"),
            self.row(name="К2", piece_price="2 679"),
            self.row(name="К3", piece_price="2679,50 сом"),
            self.row(name="К4", price_per_sqm="1 050,5"),
        ] + [self.row(name=f"Лист {i}") for i in range(21)]
        r = self.client.post(self.URL, {"rows": rows}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["created"], 25)
        k1 = Material.objects.get(name="К1")
        self.assertEqual(k1.sheet_width, Decimal("1.22"))
        self.assertEqual(k1.thickness_mm, Decimal("4.5"))
        self.assertEqual(k1.piece_price, Decimal("2679"))
        self.assertEqual(k1.piece_area, Decimal("2.9768"))
        self.assertEqual(Material.objects.get(name="К3").piece_price, Decimal("2679.50"))
        self.assertEqual(Material.objects.get(name="К4").price_per_sqm, Decimal("1050.5"))

    def test_garbage_is_reported_per_cell(self):
        r = self.client.post(self.URL, {"rows": [self.row(name="Х", piece_price="две тысячи")]}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("piece_price", r.data["errors"][0]["fields"])
