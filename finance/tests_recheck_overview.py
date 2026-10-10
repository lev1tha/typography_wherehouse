"""Перепроверка владельца 10.10, RU-N6 (S2): «Долг поставщикам» на «Обзоре» —
прирост за период (16 765), а читался как остаток (39 315). Рядом с изменением
теперь лежит сам остаток долга на сегодня — тем же расчётом, что карточка
«Долг поставщикам» в «Финансах»."""
from datetime import timedelta
from decimal import Decimal as D

from django.utils import timezone

from finance.reports.overview import headline
from finance.reports.summary import supplier_debts
from finance.tests_reports_calc import PnlCase, noon
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class PayablesBalanceTests(PnlCase):
    def test_payables_reason_carries_the_balance(self):
        sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=D("1"), sheet_height=D("1"), price_per_sqm=D("1000"),
        )
        today = timezone.localdate()
        first = today.replace(day=1)
        # Долг, взятый до периода, и ещё один — в периоде.
        receive_lot(sheet, form=Roll.Form.SHEET, area=D("10"), purchase_cost=D("30000"),
                    on_credit=True, received_at=noon(first - timedelta(days=20)), user=self.admin)
        receive_lot(sheet, form=Roll.Form.SHEET, area=D("10"), purchase_cost=D("20000"),
                    on_credit=True, user=self.admin)
        h = headline(first, today)
        row = next((r for r in h["why"]["reasons"] if r["key"] == "payables"), None)
        self.assertIsNotNone(row, h["why"]["reasons"])
        self.assertEqual(row["amount"], D("20000.00"))               # изменение за период
        self.assertEqual(row["balance"], D("50000"))                  # остаток на сегодня
        self.assertEqual(row["balance"], supplier_debts()["total"])
        self.assertEqual(row["balance_on"], today)
        for other in h["why"]["reasons"]:
            if other["key"] != "payables":
                self.assertNotIn("balance", other)
