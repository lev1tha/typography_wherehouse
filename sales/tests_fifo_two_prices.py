"""Сценарий заказчика: партия по 700 сом за лист, потом партия по 900.

Проверяем, что прибыль со старой дешёвой партии никуда не девается: подорожание
не должно ни переоценивать уже проданное, ни задирать себестоимость остатка,
закупленного дёшево, ни путать очередь списания.

Продаём ЦЕЛЫМИ ЛИСТАМИ — так заказчик и считает свой алюкобонд.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from warehouse.models import Material

SHEET = Decimal("1.22") * Decimal("2.44")   # 2.9768 кв.м в листе


class TwoPriceLotsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="fifo_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Белый алюкобонд", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            piece_price=Decimal("1500"),   # розница: 1500 за лист
        )

    def _receive(self, sheets, per_sheet, day):
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "SHEET",
            "width": "1.22", "height": "2.44", "sheet_count": str(sheets),
            "purchase_cost": str(Decimal(per_sheet) * Decimal(sheets)),
            "received_on": day,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def _sell(self, sheets):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id,
                       "mode": "PIECE", "quantity": str(sheets)}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def _per_sheet(self, receipt, sheets):
        return (receipt.cost_total / Decimal(sheets)).quantize(Decimal("1"))

    def test_old_cheap_lot_keeps_its_own_cost_after_a_pricier_intake(self):
        self._receive(20, 700, "2026-07-01")
        first = self._sell(5)
        self.assertEqual(self._per_sheet(first, 5), Decimal("700"))
        self.assertEqual(first.total_price, Decimal("7500"))
        margin_before = first.margin

        # Приехала новая партия — дороже.
        self._receive(10, 900, "2026-08-01")

        # 1. Уже проданное не переоценивается задним числом.
        first.refresh_from_db()
        self.assertEqual(self._per_sheet(first, 5), Decimal("700"),
                         "себестоимость прошлой продажи уехала на новую цену")
        self.assertEqual(first.margin, margin_before,
                         "маржа прошлого заказа изменилась после новой поставки")

        # 2. Следующая продажа берёт СТАРУЮ партию — она пришла раньше.
        second = self._sell(5)
        self.assertEqual(self._per_sheet(second, 5), Decimal("700"),
                         "продажа списала дорогую партию вместо старой дешёвой")

        # 3. Продажа на стыке: 10 старых листов + 5 новых.
        third = self._sell(15)
        self.assertEqual(third.cost_total.quantize(Decimal("1")),
                         Decimal("700") * 10 + Decimal("900") * 5,
                         "стык партий посчитан не по FIFO")

        # 4. Продали 25 из 30 листов — осталось 5, и все они из новой партии.
        self.mat.refresh_from_db()
        self.assertEqual((self.mat.quantity / SHEET).quantize(Decimal("1")), Decimal("5"))
        self.assertEqual(self.mat.stock_value.quantize(Decimal("1")), Decimal("4500"),
                         "остаток должен стоить 5 × 900, он весь из новой партии")

        # 5. Себестоимость всех продаж = закуп минус то, что лежит на складе.
        total_cogs = sum((r.cost_total for r in Receipt.objects.all()), Decimal("0"))
        bought = Decimal("700") * 20 + Decimal("900") * 10        # 23 000
        self.assertEqual(total_cogs.quantize(Decimal("1")), bought - Decimal("4500"),
                         "закуп не сошёлся: себестоимость продаж + остаток ≠ куплено")

        # 6. Последний лист берётся из новой партии — по 900.
        last = self._sell(5)
        self.assertEqual(self._per_sheet(last, 5), Decimal("900"))

    def test_stock_value_is_not_revalued_by_the_new_price(self):
        self._receive(20, 700, "2026-07-01")
        self._sell(5)
        self._receive(10, 900, "2026-08-01")
        self.mat.refresh_from_db()
        # 15 старых по 700 + 10 новых по 900
        self.assertEqual(self.mat.stock_value.quantize(Decimal("1")), Decimal("19500"))
        # Если бы считали «остаток × последняя цена» — вышло бы 25 × 900.
        self.assertNotEqual(self.mat.stock_value.quantize(Decimal("1")), Decimal("22500"))

    def test_profit_of_the_cheap_lot_survives_in_the_reports(self):
        self._receive(20, 700, "2026-07-01")
        self._sell(5)                        # 7500 выручки, 3500 себестоимости
        self._receive(10, 900, "2026-08-01")
        self._sell(5)                        # ещё 7500 / 3500 — тоже со старой

        dash = self.client.get("/api/audit/dashboard/").data["breakdown"]
        self.assertEqual(
            Decimal(str(dash["material_revenue"])).quantize(Decimal("1")), Decimal("15000")
        )
        self.assertEqual(
            Decimal(str(dash["material_cost"])).quantize(Decimal("1")), Decimal("7000")
        )
        self.assertEqual(
            Decimal(str(dash["material_profit"])).quantize(Decimal("1")), Decimal("8000"),
            "прибыль со старой дешёвой партии потерялась в «Обзоре»",
        )

        fin = self.client.get("/api/finance/report/").data
        self.assertEqual(Decimal(str(fin["cogs"])).quantize(Decimal("1")), Decimal("7000"))
        self.assertEqual(
            Decimal(str(fin["gross_margin"])).quantize(Decimal("1")), Decimal("8000")
        )
