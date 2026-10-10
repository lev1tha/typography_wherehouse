"""Перепроверка владельца 10.10 — склад и поставщики.

RS-N1/STK-04 (склад на дату одной функцией, снимок на конец месяца), G1-N2
(возврат поставщику из закрытого месяца датой возврата), F11/G3-N2 (дубли
накладных), XL-01 (минус и сантиметры в карточке), XL-06 (CSV накладных),
RS-N2 (сумма возврата в выписке после исправления прихода).

«Сегодня» — 10.10.2026, как в прогоне владельца.
"""
from datetime import date, datetime
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.material_sheet import purchases_from_stock
from finance.models import CashEntry
from finance.reports.bridge import bridge
from warehouse.models import (
    InventoryLog,
    InventoryLogLot,
    Material,
    Roll,
    StockSnapshot,
    Supplier,
    SupplierReturn,
    Supply,
    stock_value_total,
)
from warehouse.rolls import receive_lot
from warehouse.supplier_ledger import supplier_balance
from warehouse.tests_stock_on_date import freeze_now

D = Decimal
SUPPLIES = "/api/warehouse/supplies/"
ON_DATE = "/api/warehouse/materials/on-date/"
REPORT = "/api/finance/report/"
NOW = timezone.make_aware(datetime(2026, 10, 10, 12, 0))
SEP = (date(2026, 9, 1), date(2026, 9, 30))
OCT = (date(2026, 10, 1), date(2026, 10, 10))


def noon(day):
    return timezone.make_aware(datetime.combine(day, datetime.min.time().replace(hour=12)))


class Base(APITestCase):
    def setUp(self):
        freeze_now(self, NOW)
        self.admin = User.objects.create_user(username="rc_admin", password="x", role=User.Role.ADMIN)
        self.keeper = User.objects.create_user(username="rc_keeper", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        self.m = Material.objects.create(
            name="акрил прозрачный 3 мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET, sheet_width=D("1.22"), sheet_height=D("2.44"),
            price_per_sqm=D("2500"), piece_price=D("7000"), cut_rate_per_pm=D("65"),
        )
        self.sup = Supplier.objects.create(name="Пластик-Импорт")

    def sheet_line(self, count, cost):
        return {"material": self.m.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                "sheet_count": str(count), "cost": str(cost)}

    def supply(self, number, day, lines, expect=201, **extra):
        r = self.client.post(SUPPLIES, {
            "number": number, "supplier": self.sup.id, "received_on": day, "lines": lines, **extra,
        }, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def close(self, day):
        r = self.client.patch("/api/finance/period/", {"closed_through": day}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def report(self, d_from, d_to):
        return self.client.get(REPORT, {"date_from": d_from.isoformat(), "date_to": d_to.isoformat()}).data

    def on_date(self, day):
        r = self.client.get(ON_DATE, {"date": day.isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def three(self, day):
        """«Финансы», «Склад на дату», плитка отчёта — одна цифра."""
        rep = self.report(day.replace(day=1), day)
        return {
            D(str(stock_value_total(day))), D(str(self.on_date(day)["value"])),
            D(str(rep["stock"]["value_now"])),
        }


# --- RS-N1 / STK-04 -----------------------------------------------------------------------


class StockOnDateOneFunctionTests(Base):
    """test_on_date_two_formulas владельца: продали 5 листов из СВЕЖЕЙ партии B.
    Excel на 30.09: A 10 × 3000 + B 5 × 3480 = 47 400. Было: «Финансы» 49 800,
    «Склад на дату» 47 400, после октябрьской недостачи 48 360, снимок навсегда
    48 360."""

    def setUp(self):
        super().setUp()
        self.supply("A-1", "2026-09-01", [self.sheet_line(10, 30000)])
        self.supply("B-2", "2026-09-02", [self.sheet_line(10, 34800)])
        self.a = Roll.objects.get(material=self.m, purchase_cost=D("30000"))
        self.b = Roll.objects.get(material=self.m, purchase_cost=D("34800"))
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "order_date": "2026-09-10",
            "items": [{"type": "MATERIAL", "material": self.m.id, "mode": "PIECE", "quantity": 5,
                       "roll": self.b.id}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["cost_total"])), D("17400.00"))

    def shortage_in_october(self):
        r = self.client.post("/api/warehouse/materials/adjust/", {
            "material": self.m.id, "counted_sheets": "13", "happened_on": "2026-10-02",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_all_screens_agree_on_the_end_of_september(self):
        self.assertEqual(self.three(SEP[1]), {D("47400.00")})

    def test_october_shortage_does_not_move_september(self):
        self.shortage_in_october()
        self.assertEqual(self.three(SEP[1]), {D("47400.00")})
        self.assertEqual(self.three(date(2026, 10, 1)), {D("47400.00")})
        # Сейчас: A 8 × 3000 + B 5 × 3480.
        self.assertEqual(stock_value_total(), D("41400.00"))

    def test_snapshot_at_close_is_the_end_of_the_month(self):
        self.shortage_in_october()
        self.close("2026-09-30")
        snap = StockSnapshot.objects.get(as_of=SEP[1])
        self.assertEqual(snap.value, D("47400.00"))
        self.assertEqual(sum((l.value for l in snap.lines.all()), D("0")), snap.value)
        self.assertEqual(self.on_date(SEP[1])["source"], "snapshot")
        self.assertEqual(self.three(SEP[1]), {D("47400.00")})
        # Пересъёмка командой — то же число.
        out = StringIO()
        call_command("stock_snapshot", "--month", "2026-09", stdout=out)
        self.assertIn("47400.00", out.getvalue())
        self.assertEqual(StockSnapshot.objects.get(as_of=SEP[1]).value, D("47400.00"))

    def test_summary_chain_adds_up(self):
        self.shortage_in_october()
        self.close("2026-09-30")
        rec = self.report(*SEP)["stock"]["reconcile"]
        self.assertEqual(D(str(rec["value_now"])), D("47400.00"))
        self.assertEqual(D(str(rec["gap"])), D("0.00"))
        rec = self.report(*OCT)["stock"]["reconcile"]
        self.assertEqual(D(str(rec["opening"])), D("47400.00"))
        self.assertEqual(D(str(rec["losses"])), D("6000.00"))     # 2 листа со старейшей A
        self.assertEqual(D(str(rec["value_now"])), D("41400.00"))
        self.assertEqual(D(str(rec["gap"])), D("0.00"))

    def test_movements_remember_their_lots(self):
        self.shortage_in_october()
        sale = InventoryLog.objects.get(type=InventoryLog.Type.SALE)
        self.assertEqual(
            list(sale.lot_moves.values_list("roll_id", "area")), [(self.b.id, D("-14.884000"))])
        adj = InventoryLog.objects.get(type=InventoryLog.Type.ADJUSTMENT)
        self.assertEqual(list(adj.lot_moves.values_list("roll_id", "area")), [(self.a.id, D("-5.953600"))])

    def test_client_return_in_october_keeps_september(self):
        """Возврат заказа в октябре кладёт листы назад в B (запись «из каких
        партий брали» у строки при этом стирается) — 30.09 не меняется."""
        from sales.models import Receipt

        receipt = Receipt.objects.get()
        r = self.client.post(f"/api/sales/receipts/{receipt.id}/refund/", {"method": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.b.refresh_from_db()
        self.assertEqual(self.b.remaining_area, self.b.initial_area)
        self.assertEqual(self.three(SEP[1]), {D("47400.00")})
        self.assertEqual(stock_value_total(), D("64800.00"))


class FifoRecalcKeepsLotHistoryTests(Base):
    """Пересчёт FIFO переносит продажу в другую партию — раскладка записи
    журнала идёт следом, иначе склад на дату до продажи «помнил» старую."""

    def test_on_date_before_the_sale_after_recalc(self):
        a = receive_lot(self.m, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"),
                        sheet_count=D("5"), purchase_cost=D("15000"), received_at=noon(date(2026, 9, 1)))
        b = receive_lot(self.m, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"),
                        sheet_count=D("5"), purchase_cost=D("17400"), received_at=noon(date(2026, 9, 2)))
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "order_date": "2026-09-05",
            "items": [{"type": "MATERIAL", "material": self.m.id, "mode": "PIECE", "quantity": 2,
                       "roll": b.id}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.post("/api/warehouse/fifo-recalc/apply/",
                             {"material": self.m.id, "since": "2026-09-01"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        sale = InventoryLog.objects.get(type=InventoryLog.Type.SALE)
        self.assertEqual([m.roll_id for m in sale.lot_moves.all()], [a.id])
        # До продажи: обе пачки целиком.
        self.assertEqual(D(str(self.on_date(date(2026, 9, 3))["value"])), D("32400.00"))
        # После: A 3 × 3000 + B 5 × 3480.
        self.assertEqual(D(str(self.on_date(date(2026, 9, 6))["value"])), D("26400.00"))
        self.assertEqual(stock_value_total(), D("26400.00"))


# --- G1-N2: возврат поставщику из закрытого месяца ----------------------------------------


class ReturnFromClosedMonthTests(Base):
    def setUp(self):
        super().setUp()
        self.doc = self.supply("S-9", "2026-09-05", [self.sheet_line(5, 15000)])
        self.line_id = self.doc["lines"][0]["id"]

    def give_back(self, qty="1", expect=200, **extra):
        r = self.client.post(f"{SUPPLIES}{self.doc['id']}/return/", {
            "lines": [{"line": self.line_id, "quantity": qty}], "returned_on": "2026-10-10",
            "mode": "CREDIT", **extra,
        }, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def test_credit_return_goes_to_the_return_month(self):
        self.close("2026-09-30")
        sept_before = self.report(*SEP)
        data = self.give_back()
        self.assertFalse(data["return"]["in_place"])
        supply = Supply.objects.get(pk=self.doc["id"])
        # Накладная закрытого месяца не переписана.
        self.assertEqual(supply.total_cost, D("15000.00"))
        self.assertEqual(supply.lines.get().quantity, D("14.8840"))
        self.assertEqual(Roll.objects.get(material=self.m).initial_area, D("14.8840"))
        # Сентябрь не изменился ни закупом, ни складом.
        self.assertEqual(purchases_from_stock(*SEP), D("15000.00"))
        sept_after = self.report(*SEP)
        self.assertEqual(sept_after["stock"]["purchases"], sept_before["stock"]["purchases"])
        self.assertEqual(sept_after["stock"]["value_now"], sept_before["stock"]["value_now"])
        self.assertEqual(self.three(SEP[1]), {D("15000.00")})
        # Октябрь: закуп −3 000, склад меньше на лист, разрыв склада 0.
        self.assertEqual(purchases_from_stock(*OCT), D("-3000.00"))
        self.assertEqual(stock_value_total(), D("12000.00"))
        rec = self.report(*OCT)["stock"]["reconcile"]
        self.assertEqual(D(str(rec["purchases"])), D("-3000.00"))
        self.assertEqual(D(str(rec["gap"])), D("0.00"))
        self.assertEqual(bridge(*OCT)["unexplained"], D("0"))
        # Долг по накладной и сальдо поставщика — меньше на возврат.
        self.assertEqual(supply.debt, D("12000.00"))
        self.assertEqual(supplier_balance(Supplier.objects.get(pk=self.sup.pk))["saldo"], D("12000.00"))
        st = self.client.get(f"/api/warehouse/suppliers/{self.sup.id}/statement/").data
        self.assertEqual([(r["type"], D(str(r["delta"]))) for r in st["rows"]],
                         [("INVOICE", D("15000.00")), ("RETURN", D("-3000.00"))])
        self.assertEqual(D(str(st["closing_balance"])), D("12000.00"))
        # Склад: остаток −1 лист, журнал сходится с партиями.
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, D("11.9072"))
        log = InventoryLog.objects.get(type=InventoryLog.Type.CORRECTION, quantity_changed__lt=0)
        self.assertEqual(timezone.localtime(log.happened_at).date(), date(2026, 10, 10))
        self.assertEqual(log.lot_moves.get().area, D("-2.976800"))

    def test_cannot_return_more_than_left_on_the_line(self):
        self.close("2026-09-30")
        self.give_back("4")
        self.give_back("2", expect=400)
        self.give_back("1")
        supply = Supply.objects.get(pk=self.doc["id"])
        self.assertEqual(supply.returned_after, D("15000.00"))
        self.assertEqual(supply.debt, D("0"))
        self.assertEqual(stock_value_total(), D("0.00"))

    def test_refund_brings_money_back_in_october(self):
        self.doc = self.supply("S-10", "2026-09-06", [self.sheet_line(5, 15000)],
                               paid_amount="15000", paid_account="CASH", force=True)
        self.line_id = self.doc["lines"][0]["id"]
        self.assertEqual(CashEntry.balance("CASH"), D("-15000"))
        self.close("2026-09-30")
        data = self.give_back(mode="REFUND", account="CASH")
        self.assertEqual(D(str(data["return"]["refund"])), D("3000.00"))
        supply = Supply.objects.get(pk=self.doc["id"])
        self.assertEqual(supply.debt, D("0"))
        self.assertEqual(supply.overpaid, D("0"))
        self.assertEqual(CashEntry.balance("CASH"), D("-12000"))
        self.assertEqual(purchases_from_stock(*OCT), D("-3000.00"))
        self.assertEqual(bridge(*OCT)["unexplained"], D("0"))

    def test_open_month_still_corrects_in_place(self):
        self.give_back()
        supply = Supply.objects.get(pk=self.doc["id"])
        self.assertTrue(SupplierReturn.objects.get().in_place)
        self.assertEqual(supply.total_cost, D("12000.00"))
        self.assertEqual(purchases_from_stock(*SEP), D("12000.00"))
        self.assertEqual(purchases_from_stock(*OCT), D("0"))

    def test_return_date_in_a_closed_month_is_refused(self):
        self.close("2026-09-30")
        self.give_back(returned_on="2026-09-28", expect=400)

    def test_minus_invoice_points_to_the_return_button(self):
        """10e: «минусовая» накладная в обход возврата — 400 с подсказкой, а не
        «больше либо равно 0»."""
        r = self.supply("ВОЗВР-1", "2026-10-09", [self.sheet_line(1, -3000)], expect=400)
        self.assertIn("Вернуть поставщику", str(r["lines"][0]["cost"][0]))


# --- F11 / G3-N2: дубли накладных ---------------------------------------------------------


class DuplicateSupplyTests(Base):
    def setUp(self):
        super().setUp()
        self.bp = Material.objects.create(name="БП 12В", unit=Material.Unit.PIECE,
                                          price_per_unit=D("900"), purchase_price=D("400"))

    def qty(self, cost="6500"):
        return [{"material": self.bp.id, "form": "QTY", "quantity": "10", "cost": cost}]

    def test_number_lookalikes_and_nearby_dates(self):
        self.supply("Н-102", "2026-09-04", self.qty())          # кириллическая «Н»
        self.supply("H-102", "2026-09-04", self.qty(), expect=409)   # латинская «H»
        self.supply("Э-102", "2026-09-04", self.qty("6600"))
        for number, day in (("Э102", "2026-09-04"), ("э 102 ", "2026-09-04"),
                            ("Э-102", "2026-09-05"), ("Э-102", "2026-09-07"),
                            ("Э/102", "2026-09-01")):
            r = self.supply(number, day, self.qty("1"), expect=409)
            self.assertEqual(r["code"], "duplicate_supply")
        self.supply("Э-102", "2026-09-08", self.qty("1"))       # 4 дня — уже другая

    def test_without_number_the_amount_decides(self):
        first = self.supply("Э-102", "2026-09-04", self.qty())
        r = self.supply("", "2026-09-06", self.qty(), expect=409)
        self.assertEqual(r["duplicate"]["id"], first["id"])
        self.supply("", "2026-09-06", self.qty("6501"))          # другая сумма
        self.supply("", "2026-09-10", self.qty())                # 6 дней

    def test_list_marks_possible_duplicates(self):
        a = self.supply("Э-102", "2026-09-04", self.qty())
        b = self.supply("Э102", "2026-09-05", self.qty("7000"), force=True)
        c = self.supply("", "2026-09-04", self.qty(), force=True)
        solo = self.supply("Э-200", "2026-09-04", self.qty("100"))
        rows = self.client.get(SUPPLIES).data
        rows = rows["results"] if isinstance(rows, dict) else rows
        dup = {r["id"]: r["possible_duplicate_of"] for r in rows}
        self.assertIsNotNone(dup[a["id"]])
        self.assertEqual(dup[b["id"]], a["id"])
        self.assertEqual(dup[c["id"]], a["id"])
        self.assertIsNone(dup[solo["id"]])


# --- XL-01, XL-06, RS-N2 ------------------------------------------------------------------


class CardNumbersTests(Base):
    BASE = {"type": "", "thickness_mm": "3", "sheet_width": "1.22", "sheet_height": "2.44",
            "price_per_sqm": "1550", "cut_rate_per_pm": "65", "piece_price": "4614"}

    def bulk(self, **row):
        return self.client.post("/api/warehouse/materials/bulk/",
                                {"rows": [dict(self.BASE, name="М1", **row)]}, format="json")

    def test_bulk_refuses_negative_rate_and_centimetres(self):
        r = self.bulk(cut_rate_per_pm="-65")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("cut_rate_per_pm", r.data["errors"][0]["fields"])
        r = self.bulk(sheet_width="122", sheet_height="244")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("sheet_width", r.data["errors"][0]["fields"])
        self.assertFalse(Material.objects.filter(name="М1").exists())
        self.assertEqual(self.bulk(sheet_width="4", sheet_height="2.05").status_code, 201)

    def test_patch_refuses_them_too(self):
        url = f"/api/warehouse/materials/{self.m.id}/"
        self.assertEqual(self.client.patch(url, {"cut_rate_per_pm": "-10"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"sheet_width": "122"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"cut_rate_per_pm": "70"}, format="json").status_code, 200)


class SuppliesCsvTests(Base):
    def test_supplies_list_as_csv(self):
        self.supply("INV-1", "2026-10-02", [self.sheet_line(2, 7000)])
        r = self.client.get(SUPPLIES, {"export": "csv"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        text = r.content.decode("utf-8-sig")
        self.assertTrue(text.startswith("Дата;Номер;Поставщик"))
        self.assertIn("02.10.2026;INV-1;Пластик-Импорт;1;KGS;;;7000,00;0,00;0,00;7000,00", text)
        self.client.force_authenticate(self.keeper)
        text = self.client.get(SUPPLIES, {"export": "csv"}).content.decode("utf-8-sig")
        self.assertIn("02.10.2026;INV-1;Пластик-Импорт;1;KGS;;;;;;;", text)


class ReturnRepricedAfterCorrectionTests(Base):
    def test_statement_after_price_correction(self):
        doc = self.supply("INV-77", "2026-10-02", [self.sheet_line(10, 34800)])
        line_id = doc["lines"][0]["id"]
        r = self.client.post(f"{SUPPLIES}{doc['id']}/return/", {
            "lines": [{"line": line_id, "quantity": "1"}], "returned_on": "2026-10-03", "mode": "CREDIT",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        lot = Roll.objects.get(material=self.m)
        r = self.client.post("/api/warehouse/lot-correction/apply/",
                             {"roll": lot.id, "purchase_cost": "32886"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(SupplierReturn.objects.get().amount, D("3654.00"))
        st = self.client.get(f"/api/warehouse/suppliers/{self.sup.id}/statement/").data
        self.assertEqual([(r["type"], D(str(r["delta"]))) for r in st["rows"]],
                         [("INVOICE", D("36540.00")), ("RETURN", D("-3654.00"))])
        self.assertEqual(D(str(st["closing_balance"])), D("32886.00"))


class LotMovesTableTests(Base):
    def test_rows_go_with_the_journal_entry(self):
        receive_lot(self.m, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"), sheet_count=D("2"),
                    purchase_cost=D("6000"), received_at=noon(date(2026, 10, 1)))
        r = self.client.post("/api/warehouse/materials/adjust/", {
            "material": self.m.id, "counted_sheets": "1", "happened_on": "2026-10-02",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(InventoryLogLot.objects.count(), 1)
        InventoryLog.objects.filter(type=InventoryLog.Type.ADJUSTMENT).delete()
        self.assertEqual(InventoryLogLot.objects.count(), 0)
