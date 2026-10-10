"""Строка ОПиУ «Гарантийные переделки» (волна 2, D-88).

Переделка за счёт цеха: выручки нет, материал списан. Её себестоимость
переносится из «Себестоимости материала» и «Расходников» в свою строку —
итог себестоимости и валовая прибыль не меняются (двойного счёта нет).
"""
from datetime import date
from decimal import Decimal

from finance.reports import bridge as bridge_mod
from finance.reports.pnl import pnl, pnl_year
from finance.reports.summary import finance_summary
from finance.tests_reports_calc import PnlCase, noon
from sales import sale_service

D = Decimal
OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)


class WarrantyLineTests(PnlCase):
    def setUp(self):
        super().setUp()
        self.order = self.sale(date(2026, 10, 5), qty=5, paid="1500")       # 1 500 / себест. 500
        self.redo = sale_service.create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.plate,
                         "quantity": D(2), "mode": "PIECE"}],
            created_at=noon(date(2026, 10, 9)),
            is_warranty=True, warranty_of=self.order, warranty_reason="скол",
        )

    def test_cost_moves_to_own_line_total_unchanged(self):
        p = pnl(OCT1, OCT31)
        self.assertEqual(p["revenue"], D("1500"))
        self.assertEqual(p["cogs_warranty"], D("200"))
        self.assertEqual(p["cogs_material"], D("500"))
        self.assertEqual(p["cogs_services"], D("0"))
        self.assertEqual(p["cogs_total"], D("700"))                          # как и без разбивки
        self.assertEqual(p["gross_profit"], D("800"))

    def test_bridge_unexplained_is_zero(self):
        self.assertEqual(bridge_mod.bridge(OCT1, OCT31)["unexplained"], D("0"))

    def test_summary_and_work_reports(self):
        s = finance_summary(OCT1, OCT31)
        self.assertEqual(s["cogs"], D("700"))                                # склад: ушло всё
        self.assertEqual(s["materials"]["cogs_warranty"], D("200"))
        self.assertEqual(s["services"]["materials"]["cost"], D("500"))
        self.assertEqual(s["services"]["warranty"]["cost"], D("200"))

    def test_year_table_has_the_row(self):
        rows = {r["key"]: r for r in pnl_year(2026)["rows"]}
        self.assertEqual(rows["cogs_warranty"]["total"], D("-200"))
        self.assertEqual(rows["cogs"]["total"], D("-700"))
        # Пустой вид расходов «Гарантийные переделки» в таблице больше не стоит.
        self.assertFalse(any(r["label"] == "Гарантийные переделки" and r["key"].startswith("kind:")
                             for r in rows.values()))
