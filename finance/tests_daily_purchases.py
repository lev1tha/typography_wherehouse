"""Закуп по приходам — оборот, а не расход: ни в плитках, ни в графике.

Решение заказчика (2026-08-24): деньги, переложенные из кассы в склад, месяц
убыточным не делают — материал лежит на полке. Прибыль они уменьшают по мере
продажи, строкой «Себестоимость проданного». График по дням обязан жить по
тому же правилу, что плитки, иначе на одном экране две «Прибыли».
"""
from datetime import date
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import ExpenseEntry, ExpenseKind
from warehouse.models import Material, Supplier


class DailyPurchasesTests(APITestCase):
    SUPPLIES = "/api/warehouse/supplies/"
    DAILY = "/api/finance/daily/"
    REPORT = "/api/finance/report/"

    def setUp(self):
        self.admin = User.objects.create_user(username="dp_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.supplier = Supplier.objects.create(name="Глобал")
        self.sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            piece_area=Decimal("2.9768"), price_per_sqm=Decimal("1500"),
        )
        today = timezone.localdate()
        # День внутри текущего месяца, но не сегодня — чтобы видеть, что закуп
        # лёг именно на дату накладной, а не на день ввода.
        self.doc_day = date(today.year, today.month, 1) if today.day > 1 else today
        self.today = today

    def _supply(self, cost="18000", day=None):
        return self.client.post(self.SUPPLIES, {
            "number": "НК-001", "supplier": self.supplier.id,
            "received_on": (day or self.doc_day).isoformat(),
            "stated_total": cost, "paid_amount": "0",
            "lines": [{
                "material": self.sheet.id, "form": "SHEET",
                "width": "1.22", "height": "2.44", "sheet_count": "5", "cost": cost,
            }],
        }, format="json")

    def _daily(self):
        r = self.client.get(self.DAILY, {"year": self.today.year, "month": self.today.month})
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _report(self):
        r = self.client.get(self.REPORT, {"year": self.today.year, "month": self.today.month})
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_supply_document_is_working_capital_not_expense(self):
        self.assertEqual(self._supply().status_code, 201)
        daily = self._daily()
        # Закупа нет НИ В ОДНОМ дне: день прихода накладной — не провальный день.
        total_daily = sum(Decimal(str(x["variable"])) for x in daily["rows"])
        self.assertEqual(total_daily, Decimal("0"))
        # Плитки согласны: расходов нет, закуп — в секции «Склад» (оборот).
        report = self._report()
        self.assertEqual(Decimal(str(report["total_expenses"])), Decimal("0"))
        self.assertEqual(Decimal(str(report["stock"]["purchases"])), Decimal("18000"))
        self.assertEqual(Decimal(str(daily["totals"]["variable"])), Decimal(str(report["total_expenses"])))
        self.assertEqual(Decimal(str(daily["totals"]["profit"])), Decimal(str(report["profit"])))

    def test_fixed_costs_of_the_whole_month_are_in_the_chart_total(self):
        """Аренда в итоге под графиком — за ВЕСЬ месяц, как в плитке сверху.

        Итог складывался из дневных прибылей, а у будущих дней прибыли нет
        (столбик не рисуем, чтобы 20-е число не краснело за неотработанную
        аренду) — и их доля аренды выпадала. 17 августа на одном экране стояло
        −49 497 в плитке и −35 949 под графиком.
        """
        self.assertEqual(self._supply().status_code, 201)
        rent = ExpenseKind.objects.get(code="RENT")
        ExpenseEntry.objects.create(
            kind=rent, name="Аренда цеха", amount=Decimal("30000"), spent_at=self.doc_day
        )
        daily = self._daily()
        report = self._report()
        self.assertEqual(Decimal(str(daily["totals"]["fixed"])), Decimal("30000"))
        self.assertEqual(
            Decimal(str(daily["totals"]["profit"])), Decimal(str(report["profit"]))
        )
        # Столбики по-прежнему знают только про прошедшие дни — это не итог.
        drawn = [r for r in daily["rows"] if r["profit"] is not None]
        self.assertLessEqual(len(drawn), daily["days_in_month"])

    def test_kind_flag_is_respected_like_in_the_tiles(self):
        """Снятый флаг «уменьшает прибыль» у вида «Закуп материала» убирает
        закуп из прибыли в плитках — и в графике тоже, иначе снова две цифры."""
        self.assertEqual(self._supply().status_code, 201)
        ExpenseKind.objects.filter(code=ExpenseKind.MATERIAL_PURCHASE).update(in_profit=False)
        daily = self._daily()
        report = self._report()
        self.assertEqual(Decimal(str(daily["totals"]["variable"])), Decimal("0"))
        self.assertEqual(Decimal(str(daily["totals"]["variable"])), Decimal(str(report["total_expenses"])))
