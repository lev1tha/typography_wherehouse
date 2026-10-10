"""«Резка по станкам» целиком и выручка по видам услуг (STAFF-03/-04, STAFF-14, PNL-05/-06).

Суммы посчитаны руками. Октябрь 2026, строки услуг:
  05.10  резка на ЧПУ 5 000 (50 пог.м)
  05.10  гравировка (станок «лазер») 1 000
  06.10  резка на лазере 2 000 (20 пог.м)
  07.10  монтаж (без станка) 2 500
  08.10  отходы 800 — не работа станка
  09.10  резка на ЧПУ 3 000 — вернули 10.10
"""
from datetime import date
from decimal import Decimal

from django.utils import timezone

from finance.models import FinanceSettings
from finance.reports.summary import finance_summary
from finance.tests_reports_calc import PnlCase, noon
from sales.models import TransactionItem
from services.models import PricingSettings, PrintingService

D = Decimal
OCT, OCT_END = date(2026, 10, 1), date(2026, 10, 31)


class WorkReportCase(PnlCase):
    def setUp(self):
        super().setUp()
        Kind, Machine = PrintingService.Kind, PrintingService.Machine
        self.cnc = PrintingService.objects.create(name="Резка ЧПУ", kind=Kind.CUTTING, machine=Machine.CNC)
        self.laser = PrintingService.objects.create(name="Резка лазер", kind=Kind.CUTTING, machine=Machine.LASER)
        self.engrave = PrintingService.objects.create(name="Гравировка", kind=Kind.ENGRAVING, machine=Machine.LASER)
        self.install = PrintingService.objects.create(name="Монтаж", kind=Kind.INSTALLATION)
        self.waste = PrintingService.objects.create(name="Отходы", kind=Kind.WASTE)

    def line(self, day, service, qty, price, cost="0", returned_on=None):
        receipt = self.sale(day)
        item = TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.SERVICE, service=service,
            quantity=D(qty), price_per_item=D(price), cost_total=D(cost))
        if returned_on:
            TransactionItem.objects.filter(pk=item.pk).update(
                is_returned=True, returned_at=noon(returned_on))
        return item

    def october(self):
        self.line(date(2026, 10, 5), self.cnc, 50, 100)
        self.line(date(2026, 10, 5), self.engrave, 1, 1000)
        self.line(date(2026, 10, 6), self.laser, 20, 100)
        self.line(date(2026, 10, 7), self.install, 1, 2500)
        self.line(date(2026, 10, 8), self.waste, 1, 800)
        self.line(date(2026, 10, 9), self.cnc, 30, 100, returned_on=date(2026, 10, 10))

    def cutting(self):
        return finance_summary(OCT, OCT_END)["cutting"]

    def rows(self):
        return {r["id"]: r for r in self.cutting()["rows"]}


class MachineTableTests(WorkReportCase):
    def test_other_works_get_their_own_column(self):
        self.october()
        rows = self.rows()
        self.assertEqual(rows["LASER"]["amount"], D("2000"))            # резка лазера
        self.assertEqual(rows["LASER"]["other_amount"], D("1000"))      # гравировка на лазере
        self.assertEqual(rows[None]["other_amount"], D("2500"))         # монтаж — «Без станка»
        self.assertEqual(self.cutting()["other_total"], D("3500"))      # отходы сюда не входят

    def test_return_is_a_column_and_the_machine_stays(self):
        self.october()
        cnc = self.rows()["CNC"]
        self.assertEqual((cnc["sold"], cnc["returned"], cnc["amount"]), (D("8000"), D("3000"), D("5000")))
        # Резка, проданная в периоде и вернувшаяся в нём, — нетто 0, но станок виден.
        Machine = PrintingService.Machine
        self.line(date(2026, 10, 20), self.laser, 10, 100, returned_on=date(2026, 10, 21))
        laser = self.rows()["LASER"]
        self.assertEqual((laser["sold"], laser["returned"], laser["amount"]), (D("3000"), D("1000"), D("2000")))
        self.assertTrue(Machine.CNC)

    def test_fully_returned_machine_is_not_erased(self):
        self.line(date(2026, 10, 9), self.cnc, 30, 100, returned_on=date(2026, 10, 10))
        row = self.rows()["CNC"]
        self.assertEqual((row["sold"], row["returned"], row["amount"]), (D("3000"), D("3000"), D("0")))

    def test_totals_still_tie_with_the_old_cutting_total(self):
        self.october()
        c = self.cutting()
        self.assertEqual(c["total"], sum(r["amount"] for r in c["rows"]))
        self.assertEqual(c["total"], D("7000"))                         # 5 000 + 2 000 (3 000 вернули)

    def test_days_row_by_day(self):
        self.october()
        days = {d["date"]: d for d in self.cutting()["days"]}
        d5 = days[date(2026, 10, 5)]
        self.assertEqual((d5["machines"]["CNC"], d5["other"], d5["total"]), (D("5000"), D("1000"), D("6000")))
        d10 = days[date(2026, 10, 10)]                                  # возврат того дня, когда его оформили
        self.assertEqual((d10["returned"], d10["total"]), (D("3000"), D("-3000")))
        self.assertNotIn(date(2026, 10, 8), days)                       # день с одними отходами пуст
        self.assertEqual(sum(d["total"] for d in days.values()), D("7000") + D("3500"))


class MasterShareRoundingTests(PnlCase):
    def test_half_goes_up(self):
        """5 % от 50 = 2,5 → 3 (банковское округление давало 2)."""
        from finance.models import TaxRate

        TaxRate.objects.all().delete()
        cnc = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC)
        receipt = self.sale(date(2026, 10, 5))
        TransactionItem.objects.create(receipt=receipt, type="SERVICE", service=cnc,
                                       quantity=D("1"), price_per_item=D("50"))
        settings = PricingSettings.load()
        settings.master_commission_percent = D("5")
        settings.save()
        self.assertEqual(finance_summary(OCT, OCT_END)["cutting"]["master_share"], D("3"))


class ByServiceTests(WorkReportCase):
    def test_revenue_and_margin_by_service_group(self):
        self.line(date(2026, 10, 5), self.cnc, 50, 100, cost="500")
        self.line(date(2026, 10, 6), self.engrave, 1, 1000, cost="100")
        self.line(date(2026, 10, 7), self.install, 1, 2500)
        self.line(date(2026, 10, 8), self.waste, 1, 800)
        self.line(date(2026, 10, 9), self.cnc, 30, 100, returned_on=date(2026, 10, 10))
        block = finance_summary(OCT, OCT_END)["services"]
        by = {r["key"]: r for r in block["rows"]}
        self.assertEqual((by["cutting"]["revenue"], by["cutting"]["cost"], by["cutting"]["margin"]),
                         (D("5000"), D("500"), D("4500")))
        self.assertEqual(by["engraving"]["margin"], D("900"))
        self.assertEqual(by["install"]["revenue"], D("2500"))
        self.assertEqual(by["waste"]["revenue"], D("800"))
        self.assertNotIn("letters", by)
        self.assertFalse(block["master_share_included"])

    def test_services_inside_a_group_are_listed_separately(self):
        """Прямой и кривой рез — разные услуги станка: каждая своей строкой."""
        curved = PrintingService.objects.create(
            name="Фигурная резка ЧПУ", kind=PrintingService.Kind.CUTTING, machine="CNC")
        self.line(date(2026, 10, 5), self.cnc, 10, 100)
        self.line(date(2026, 10, 5), curved, 10, 200)
        block = finance_summary(OCT, OCT_END)["services"]
        cutting = next(r for r in block["rows"] if r["key"] == "cutting")
        self.assertEqual({s["name"]: s["revenue"] for s in cutting["services"]},
                         {"Резка ЧПУ": D("1000"), "Фигурная резка ЧПУ": D("2000")})

    def test_master_share_is_off_by_default_and_subtracts_when_enabled(self):
        self.line(date(2026, 10, 5), self.cnc, 50, 100, cost="500")      # 5 000
        settings = PricingSettings.load()
        settings.master_commission_percent = D("4")
        settings.save()
        off = next(r for r in finance_summary(OCT, OCT_END)["services"]["rows"] if r["key"] == "cutting")
        self.assertEqual((off["master_share"], off["margin"]), (D("0"), D("4500")))
        fs = FinanceSettings.load()
        fs.master_share_in_margin = True
        fs.save()
        on = finance_summary(OCT, OCT_END)["services"]
        row = next(r for r in on["rows"] if r["key"] == "cutting")
        self.assertEqual((row["master_share"], row["margin"]), (D("200.00"), D("4300.00")))   # 4 % × 5 000
        self.assertTrue(on["master_share_included"])

    def test_pnl_does_not_change_with_the_margin_setting(self):
        """Настройка касается только отчётов маржи: ОПиУ тот же."""
        from finance.reports.pnl import pnl

        self.line(date(2026, 10, 5), self.cnc, 50, 100, cost="500")
        before = pnl(OCT, OCT_END)
        fs = FinanceSettings.load()
        fs.master_share_in_margin = True
        fs.save()
        self.assertEqual(pnl(OCT, OCT_END), before)
