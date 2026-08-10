"""Обзор: прибыль с материала = продали − себестоимость проданного.

Одна цифра «выручка с материала» не отвечала на вопрос, сколько на нём
заработали: продали на 149 232 — а купили почём? Теперь рядом стоит
себестоимость (снимок закупки на момент списания, для рулонных — по FIFO) и
разница между ними.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from sales.sale_service import create_sale, refund_receipt
from warehouse.models import Material


class DashboardMaterialProfitTests(APITestCase):
    URL = "/api/audit/dashboard/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_mp", password="x", role=User.Role.ADMIN
        )
        # Лист за 1000, закупка 600 → на каждом листе зарабатываем 400.
        self.material = Material.objects.create(
            name="Форекс маржа",
            unit=Material.Unit.SQM,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0"),
            purchase_price=Decimal("600"),
            piece_price=Decimal("1000"),
            piece_area=Decimal("1"),
        )
        self.client_one = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Покупатель", phone="+996700333"
        )

    def _sale(self, *, sheets=1, paid=True):
        total = Decimal("1000") * sheets
        return create_sale(
            client=self.client_one,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {
                    "type": "MATERIAL",
                    "material": self.material,
                    "quantity": sheets,
                    "mode": "PIECE",
                }
            ],
            amount_paid=total if paid else Decimal("0"),
        )

    def _breakdown(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data["breakdown"]

    def test_profit_is_revenue_minus_cost(self):
        self._sale(sheets=3)
        b = self._breakdown()
        self.assertEqual(Decimal(str(b["material_revenue"])), Decimal("3000"))
        self.assertEqual(Decimal(str(b["material_cost"])), Decimal("1800"))
        self.assertEqual(Decimal(str(b["material_profit"])), Decimal("1200"))

    def test_cost_covers_the_same_lines_as_revenue(self):
        """Себестоимость снимается с ТЕХ ЖЕ строк: неоплаченный заказ не должен
        попасть в себестоимость, раз его выручки в цифре нет."""
        self._sale(sheets=2)  # оплачен
        self._sale(sheets=5, paid=False)  # в долг — в выручку не идёт
        b = self._breakdown()
        self.assertEqual(Decimal(str(b["material_revenue"])), Decimal("2000"))
        self.assertEqual(Decimal(str(b["material_cost"])), Decimal("1200"))
        self.assertEqual(Decimal(str(b["material_profit"])), Decimal("800"))

    def test_returned_lines_drop_out_of_both(self):
        """Возврат убирает строку и из выручки, и из себестоимости — иначе
        прибыль ушла бы в минус на стоимость вернувшегося материала."""
        keep = self._sale(sheets=1)
        back = self._sale(sheets=1)
        refund_receipt(back, user=self.admin)

        b = self._breakdown()
        self.assertEqual(Decimal(str(b["material_revenue"])), Decimal("1000"))
        self.assertEqual(Decimal(str(b["material_cost"])), Decimal("600"))
        self.assertEqual(Decimal(str(b["material_profit"])), Decimal("400"))
        self.assertEqual(keep.items.filter(is_returned=False).count(), 1)

    def test_no_sales_gives_zeroes_not_errors(self):
        b = self._breakdown()
        self.assertEqual(Decimal(str(b["material_revenue"])), Decimal("0"))
        self.assertEqual(Decimal(str(b["material_cost"])), Decimal("0"))
        self.assertEqual(Decimal(str(b["material_profit"])), Decimal("0"))

    def test_period_filter_applies_to_cost_too(self):
        """Период двигает обе части формулы, а не одну: иначе за узкий период
        выручка была бы нулевой, а себестоимость — полной, и прибыль ушла в минус."""
        self._sale(sheets=2)
        self.client.force_authenticate(self.admin)
        resp = self.client.get(self.URL, {"date_from": "2000-01-01", "date_to": "2000-01-31"})
        self.assertEqual(resp.status_code, 200, resp.data)
        b = resp.data["breakdown"]
        self.assertEqual(Decimal(str(b["material_revenue"])), Decimal("0"))
        self.assertEqual(Decimal(str(b["material_cost"])), Decimal("0"))
        self.assertEqual(Decimal(str(b["material_profit"])), Decimal("0"))
