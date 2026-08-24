"""Секция «Склад (оборот)» в финотчёте: деньги в материале — не расход.

Закуп попадает сюда, а не в «Расходы»; стоимость склада — по ценам партий
(штучные и кг/л — по закупочной из карточки). Прибыль материал уменьшает по
мере продажи, строкой «Себестоимость проданного».
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.sale_service import create_sale
from warehouse.models import Material
from warehouse.rolls import receive_lot


class StockSectionTests(APITestCase):
    REPORT = "/api/finance/report/"

    def setUp(self):
        self.admin = User.objects.create_user(username="ss_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"),
        )
        # Партия 10 кв.м за 2000 (200/кв.м) + штучный: 50 шт по 30.
        receive_lot(self.sheet, form="ROLL", width=Decimal("1"), length=Decimal("10"),
                    purchase_cost=Decimal("2000"))
        self.piece = Material.objects.create(
            name="Саморез", unit=Material.Unit.PIECE,
            quantity=Decimal("50"), purchase_price=Decimal("30"),
        )

    def _report(self):
        r = self.client.get(self.REPORT)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_stock_value_counts_lots_and_pieces(self):
        data = self._report()
        # 10 × 200 (партия) + 50 × 30 (штучный) = 3500.
        self.assertEqual(Decimal(str(data["stock"]["value_now"])), Decimal("3500.00"))
        self.assertEqual(Decimal(str(data["stock"]["purchases"])), Decimal("2000"))
        # Ввод склада не сделал месяц убыточным: расходов нет.
        self.assertEqual(Decimal(str(data["total_expenses"])), Decimal("0"))
        self.assertEqual(Decimal(str(data["profit"])), Decimal("0"))

    def test_sale_moves_cost_from_stock_to_expenses(self):
        create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{
                "type": "MATERIAL", "material": self.sheet,
                "quantity": Decimal("2"), "mode": "SQM",
            }],
            amount_paid=Decimal("0"),
        )
        data = self._report()
        # Продали 2 кв.м: себестоимость 400 ушла в расходы…
        self.assertEqual(Decimal(str(data["materials"]["cogs"])), Decimal("400"))
        self.assertEqual(Decimal(str(data["total_expenses"])), Decimal("400"))
        # …и ровно на неё похудел склад: 3500 − 400 = 3100.
        self.assertEqual(Decimal(str(data["stock"]["value_now"])), Decimal("3100.00"))
        # Прибыль = выручка (2 × 1000) − себестоимость.
        self.assertEqual(Decimal(str(data["profit"])), Decimal("1600"))

    def test_hidden_material_does_not_inflate_stock_value(self):
        """Скрытый штучный материал в стоимости полок не считается."""
        self.piece.is_archived = True
        self.piece.save(update_fields=["is_archived"])
        self.assertEqual(
            Decimal(str(self._report()["stock"]["value_now"])), Decimal("2000.00")
        )
