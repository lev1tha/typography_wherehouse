"""Секция «Склад (оборот)» в финотчёте: деньги в материале — не расход.

Закуп попадает сюда, а не в «Расходы»; стоимость склада — по ценам партий
(штучные без партий — по закупочной из карточки), той же формулой, что в
«Обзоре». Прибыль материал уменьшает по мере продажи, строкой «Себестоимость
проданного».
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
        # Продали 2 кв.м: себестоимость 400 ушла со склада в оборот. В РАСХОДАХ
        # её нет (решение владельца, 2026-08-27) — она вычитается сверху, при
        # переходе от выручки к валовой прибыли.
        self.assertEqual(Decimal(str(data["materials"]["cogs"])), Decimal("400"))
        self.assertEqual(Decimal(str(data["total_expenses"])), Decimal("0"))
        # …и ровно на неё похудел склад: 3500 − 400 = 3100.
        self.assertEqual(Decimal(str(data["stock"]["value_now"])), Decimal("3100.00"))
        # Прибыль = выручка (2 × 1000) − себестоимость.
        self.assertEqual(Decimal(str(data["profit"])), Decimal("1600"))

    def _dashboard_asset(self):
        r = self.client.get("/api/audit/dashboard/")
        self.assertEqual(r.status_code, 200, r.data)
        return Decimal(str(r.data["unrealised_asset"]))

    def test_hidden_material_with_stock_still_counts(self):
        """Скрытый материал с остатком лежит на полке — и в стоимости склада.

        Раньше «Финансы» его выкидывали, а «Обзор» считал (решение 18.08:
        «Удалить» не должно мгновенно уменьшать активы) — одна цифра на двух
        экранах была разной.
        """
        self.piece.is_archived = True
        self.piece.save(update_fields=["is_archived"])
        self.assertEqual(
            Decimal(str(self._report()["stock"]["value_now"])), Decimal("3500.00")
        )
        self.assertEqual(self._dashboard_asset(), Decimal("3500.00"))

    def test_piece_lots_are_not_counted_twice(self):
        """Штучная партия — один раз, по своей цене.

        Финотчёт складывал остатки ВСЕХ партий и сверху количество × закупочную
        у штучных. Приход штучной партии поднимает `quantity`, так что те же
        штуки попадали в сумму дважды: на проде «Склад (оборот)» был больше
        «Стоимости склада» в «Обзоре» на двести с лишним тысяч.
        """
        glue = Material.objects.create(name="Клей", unit=Material.Unit.PIECE)
        receive_lot(glue, form="PIECE", sheet_count=Decimal("10"),
                    purchase_cost=Decimal("1000"))   # 10 шт по 100
        receive_lot(glue, form="PIECE", sheet_count=Decimal("10"),
                    purchase_cost=Decimal("3000"))   # 10 шт по 300
        # Старая партия не дорожает от новой: 10 × 100 + 10 × 300 = 4000,
        # а не 20 × 300 (цена последнего прихода) и не 4000 + 6000 (дважды).
        glue.refresh_from_db()
        self.assertEqual(glue.stock_value, Decimal("4000.00"))
        # 3500 (акрил + саморезы) + 4000 (клей).
        self.assertEqual(
            Decimal(str(self._report()["stock"]["value_now"])), Decimal("7500.00")
        )
        self.assertEqual(self._dashboard_asset(), Decimal("7500.00"))
