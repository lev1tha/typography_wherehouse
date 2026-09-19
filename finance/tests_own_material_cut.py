"""Резка материала клиента: работа есть, материала на складе нет.

Проверка прод-данных 19.09.2026: «Резка, всего» в отчёте показывала 84 363, а
столбец «Резка» в таблице по материалам — 83 075. Разницу в 1 288 давали три
заказа, где резали СВОЙ материал клиента: строки материала в чеке нет, отнести
работу не к чему, и сумма просто выпадала из таблицы. Деньги при этом получены,
и в таблице заказчика они стоять обязаны — отдельной строкой.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from sales.sale_service import create_sale
from services.models import PrintingService
from warehouse.models import Material


class OwnMaterialCutTests(APITestCase):
    SHEET = "/api/finance/material-report/"
    REPORT = "/api/finance/report/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="om_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.material = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, quantity=Decimal("500"),
            is_roll_material=True, price_per_sqm=Decimal("1000"),
            purchase_price=Decimal("600"), cut_rate_per_pm=Decimal("50"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC,
        )

    def _cut(self, *, with_material, running_meters="10"):
        # У резки своего материала ставку берут не из карточки (материала-то в
        # заказе нет) — её проставляет кассир, как на проде.
        items = [{
            "type": "SERVICE", "service": self.cutting, "material": self.material,
            "width": "2", "length": "3", "running_meters": running_meters,
            **({} if with_material else {"own_material": True, "cut_rate": Decimal("50")}),
        }]
        if with_material:
            items.append({
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal("6"), "mode": "SQM",
            })
        return create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=items, amount_paid=None,
        )

    def _sheet(self):
        r = self.client.get(self.SHEET)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_own_material_cut_gets_its_own_row(self):
        self._cut(with_material=False)
        rows = self._sheet()["rows"]
        own = [r for r in rows if r["id"] is None]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["cut_revenue"], Decimal("500"))
        # Складских колонок у такой строки нет: со склада ничего не уходило.
        self.assertEqual(own[0]["material_revenue"], Decimal("0"))
        self.assertEqual(own[0]["sold_area"], Decimal("0"))
        self.assertEqual(own[0]["unit"], "")

    def test_sheet_column_equals_the_cutting_tile(self):
        """Главная проверка: столбец «Резка» == «Резка, всего» в отчёте."""
        self._cut(with_material=True)
        self._cut(with_material=False, running_meters="4")
        sheet = self._sheet()
        cutting = self.client.get(self.REPORT).data["cutting"]
        self.assertEqual(
            Decimal(str(sheet["totals"]["cut_revenue"])), Decimal(str(cutting["total"]))
        )

    def test_such_orders_are_counted_in_the_total(self):
        """Чек без строки материала — тоже заказ периода."""
        self._cut(with_material=True)
        self._cut(with_material=False)
        self.assertEqual(self._sheet()["totals"]["orders"], 2)

    def test_no_extra_row_when_every_cut_has_our_material(self):
        self._cut(with_material=True)
        self.assertFalse([r for r in self._sheet()["rows"] if r["id"] is None])
