"""S3 перепроверки владельца, RU-N20: нехватка склада в кассе — в единицах
материала («нужно 50 листов, есть 10 листов»), без семи знаков после точки,
и с кодом `stock_short`, по которому касса закрывает «Оформить»."""
import re
from decimal import Decimal as D

from sales.tests_calc_base import CalcBase
from warehouse.models import Material


class StockShortTextTests(CalcBase):
    def assert_short(self, response, *parts):
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data.get("code"), "stock_short", response.data)
        text = response.data["detail"]
        self.assertIsNone(re.search(r"\d+\.\d{4,}", text), text)
        for part in parts:
            self.assertIn(part, text)

    def test_sheets_are_counted_in_sheets(self):
        item = {"type": "MATERIAL", "material": self.forex3.id, "mode": "PIECE", "quantity": "50"}
        self.assert_short(self.preview([item]), "нужно 50 листов", "есть 10 листов", "форекс 3 мм")
        self.assert_short(self.co([item]), "нужно 50 листов")

    def test_area_in_square_metres(self):
        item = {"type": "MATERIAL", "material": self.acr3.id, "mode": "SQM", "quantity": "40"}
        self.assert_short(self.preview([item]), "нужно 40 кв.м", "есть 29.76 кв.м")

    def test_piece_material_in_its_unit(self):
        glue = Material.objects.create(name="Клей", unit=Material.Unit.PIECE, quantity=D("3"),
                                       price_per_unit=D("10"), purchase_price=D("4"))
        item = {"type": "MATERIAL", "material": glue.id, "mode": "PIECE", "quantity": "5"}
        self.assert_short(self.preview([item]), "нужно 5", "есть 3")
