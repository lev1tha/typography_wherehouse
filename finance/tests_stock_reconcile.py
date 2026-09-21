"""«Куда делись деньги склада» — закуп минус проданное минус списанное.

Проверка прод-данных 19.09.2026: закуплено на 1 678 477, продано по
себестоимости на 309 261, на полках 1 239 959 — и 129 257 разницы, которую
объяснить было нечем. Оказалось: материал выносили со склада правкой остатка
(«инвентаризация»), и эти деньги не попадали ни в себестоимость, ни в расходы —
просто исчезали. Теперь потери стоят своей строкой, а необъяснённый остаток
показан цифрой, а не спрятан в разнице двух других.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.sale_service import create_sale
from warehouse.models import InventoryLog, Material
from warehouse.rolls import consume_area, receive_lot


class StockReconcileTests(APITestCase):
    REPORT = "/api/finance/report/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="sr_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"),
        )
        # Партия 10 кв.м за 2000 — по 200 за квадрат.
        receive_lot(self.sheet, form="ROLL", width=Decimal("1"), length=Decimal("10"),
                    purchase_cost=Decimal("2000"))

    def _stock(self):
        r = self.client.get(self.REPORT)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["stock"]

    def test_everything_accounted_for_adds_up(self):
        """Купили 2000, продали на 400, списали на 200 — на полке 1400, разрыв 0."""
        create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{
                "type": "MATERIAL", "material": self.sheet,
                "quantity": Decimal("2"), "mode": "SQM",
            }],
            amount_paid=Decimal("0"),
        )
        consume_area(
            self.sheet, Decimal("1"), user=self.admin, reason="Инвентаризация",
            log_type=InventoryLog.Type.ADJUSTMENT,
        )
        stock = self._stock()
        self.assertEqual(Decimal(str(stock["losses"])), Decimal("200.00"))
        rec = stock["reconcile"]
        # Периода не задали — цепочка за всю историю, и начинается с нуля.
        self.assertEqual(Decimal(str(rec["opening"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(rec["purchases"])), Decimal("2000.00"))
        self.assertEqual(Decimal(str(rec["cogs"])), Decimal("400.00"))
        self.assertEqual(Decimal(str(rec["losses"])), Decimal("200.00"))
        self.assertEqual(Decimal(str(rec["expected"])), Decimal("1400.00"))
        self.assertEqual(Decimal(str(rec["value_now"])), Decimal("1400.00"))
        self.assertEqual(Decimal(str(rec["gap"])), Decimal("0.00"))

    def test_loss_without_a_recorded_cost_is_counted_separately(self):
        """Старые списания (до 04.09) себестоимости не несут — их не выдумываем."""
        consume_area(
            self.sheet, Decimal("1"), user=self.admin, reason="Инвентаризация",
            log_type=InventoryLog.Type.ADJUSTMENT,
        )
        InventoryLog.objects.filter(type=InventoryLog.Type.ADJUSTMENT).update(cost=None)
        stock = self._stock()
        self.assertEqual(Decimal(str(stock["losses"])), Decimal("0"))
        self.assertEqual(stock["losses_unknown"], 1)
        rec = stock["reconcile"]
        self.assertEqual(rec["losses_unknown"], 1)
        # Деньги списанного не учтены нигде — и разрыв честно это показывает.
        self.assertEqual(Decimal(str(rec["gap"])), Decimal("200.00"))

    def test_stock_left_over_from_before_the_system_shows_as_a_gap(self):
        """Остаток, заведённый правкой без прихода, — разрыв в минус."""
        piece = Material.objects.create(
            name="Диод", unit=Material.Unit.PIECE,
            quantity=Decimal("0"), purchase_price=Decimal("4"),
        )
        r = self.client.post(
            "/api/warehouse/materials/adjust/",
            {"material": piece.id, "counted_quantity": "800"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        rec = self._stock()["reconcile"]
        # 800 штук по 4 сома появились на складе, закупа под них нет.
        self.assertEqual(Decimal(str(rec["gap"])), Decimal("-3200.00"))
        # И разрыв не просто показан, а назван: вот эти самые 3 200.
        self.assertEqual(Decimal(str(rec["stock_without_lots"])), Decimal("3200.00"))

    def test_stock_backed_by_lots_is_not_counted_as_unexplained(self):
        """Материал, пришедший партией, «остатком без прихода» не считается."""
        rec = self._stock()["reconcile"]
        self.assertEqual(Decimal(str(rec["stock_without_lots"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(rec["gap"])), Decimal("0.00"))

    def test_reconcile_follows_the_period(self):
        """Цепочка идёт по выбранному периоду: в месяце без движений всё нули.

        Раньше блок считался за всю историю, потому что остаток система знала
        только «на сейчас». Из-за этого в пустом месяце строки стояли с
        суммами за год, а рядом плитки показывали нули.
        """
        stock = self.client.get(
            self.REPORT, {"date_from": "2020-01-01", "date_to": "2020-01-31"}
        ).data["stock"]
        rec = stock["reconcile"]
        self.assertEqual(Decimal(str(stock["purchases"])), Decimal("0"))
        for key in ("opening", "purchases", "cogs", "losses", "expected", "value_now", "gap"):
            self.assertEqual(Decimal(str(rec[key])), Decimal("0"), key)
