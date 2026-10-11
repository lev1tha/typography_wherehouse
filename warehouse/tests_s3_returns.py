"""S3 перепроверки владельца 10.10 — возврат поставщику в «Движении» (RU-N23).

Было: у накладной ОТКРЫТОГО месяца возврат переписывал запись прихода
(«Поступление: 2 листа … после возврата поставщику»), а рядом стояла запись
«Возврат поставщику» с нулём — в журнале нельзя было увидеть, сколько пришло по
бумаге и сколько уехало назад. Стало, как у накладной закрытого месяца (D-171) и
как в Excel: приход остаётся как принят по бумаге, возврат — отдельной строкой с
минусом и стоимостью датой возврата. Остатки, партии, склад на дату, закуп,
сальдо и «Не объяснено» сходятся.
"""
from datetime import date
from decimal import Decimal

from django.utils import timezone

from finance.material_sheet import collect_flows, purchases_from_stock
from finance.models import CashEntry
from finance.reports.bridge import bridge
from warehouse.models import InventoryLog, Roll, Supplier, SupplierReturn, Supply, stock_value_total
from warehouse.supplier_ledger import supplier_balance
from warehouse.tests_recheck_stock import OCT, SUPPLIES, Base

D = Decimal
SHEET = D("2.9768")


class OpenMonthReturnTests(Base):
    def setUp(self):
        super().setUp()
        self.doc = self.supply("S-1", "2026-10-02", [self.sheet_line(3, 25200)],
                               paid_amount="25200", paid_account="BANK")
        self.line_id = self.doc["lines"][0]["id"]

    def give_back(self, qty="1", expect=200, **extra):
        body = {"lines": [{"line": self.line_id, "quantity": qty}], "returned_on": "2026-10-08",
                "mode": "REFUND", "account": "BANK", **extra}
        r = self.client.post(f"{SUPPLIES}{self.doc['id']}/return/", body, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def test_intake_stays_as_on_paper_and_the_return_is_its_own_row(self):
        data = self.give_back()
        self.assertFalse(data["return"]["in_place"])
        intake = InventoryLog.objects.get(type=InventoryLog.Type.SUPPLY, material=self.m)
        self.assertEqual(intake.quantity_changed, 3 * SHEET)
        self.assertNotIn("после возврата", intake.reason)
        back = InventoryLog.objects.get(type=InventoryLog.Type.CORRECTION, material=self.m)
        self.assertEqual(back.quantity_changed, -SHEET)
        self.assertEqual(back.cost, D("8400.00"))
        self.assertEqual(timezone.localtime(back.happened_at).date(), date(2026, 10, 8))
        self.assertTrue(back.reason.startswith("Возврат поставщику"))
        self.assertEqual(back.lot_moves.get().area, -SHEET)
        # Движение по API: приход +3 листа, возврат −1 лист со стоимостью.
        rows = self.client.get("/api/warehouse/inventory-logs/", {"material": self.m.id}).data
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        got = sorted((r["type"], D(str(r["quantity_changed"]))) for r in rows)
        self.assertEqual(got, [("CORRECTION", -SHEET), ("SUPPLY", 3 * SHEET)])

    def test_stock_lots_journal_and_paper_agree(self):
        self.give_back()
        self.m.refresh_from_db()
        lot = Roll.objects.get(material=self.m)
        journal = sum((e.quantity_changed for e in InventoryLog.objects.filter(material=self.m)), D("0"))
        self.assertEqual(self.m.quantity, 2 * SHEET)
        self.assertEqual(lot.remaining_area, 2 * SHEET)
        self.assertEqual(journal, 2 * SHEET)
        # Партия и строка — как в бумаге: 3 листа за 25 200.
        self.assertEqual((lot.initial_area, lot.purchase_cost), (3 * SHEET, D("25200.00")))
        supply = Supply.objects.get(pk=self.doc["id"])
        self.assertEqual(supply.total_cost, D("25200.00"))
        self.assertEqual(supply.lines.get().quantity, 3 * SHEET)
        self.assertEqual(supply.returned_after, D("8400.00"))

    def test_money_purchases_stock_and_unexplained(self):
        self.give_back()
        supply = Supply.objects.get(pk=self.doc["id"])
        self.assertEqual((supply.debt, supply.overpaid), (D("0"), D("0")))
        self.assertEqual(CashEntry.balance("BANK"), D("-16800"))
        self.assertEqual(purchases_from_stock(*OCT), D("16800.00"))
        self.assertEqual(stock_value_total(), D("16800.00"))
        self.assertEqual(self.three(date(2026, 10, 7)), {D("25200.00")})
        self.assertEqual(self.three(date(2026, 10, 10)), {D("16800.00")})
        rec = self.report(*OCT)["stock"]["reconcile"]
        self.assertEqual(D(str(rec["gap"])), D("0.00"))
        self.assertEqual(bridge(*OCT)["unexplained"], D("0"))
        self.assertEqual(supplier_balance(Supplier.objects.get(pk=self.sup.pk))["saldo"], D("0.00"))
        st = self.client.get(f"/api/warehouse/suppliers/{self.sup.id}/statement/").data
        types = [(r["type"], D(str(r["delta"]))) for r in st["rows"]]
        self.assertIn(("INVOICE", D("25200.00")), types)
        self.assertIn(("RETURN", D("-8400.00")), types)
        self.assertEqual(D(str(st["closing_balance"])), D("0"))
        # Складской лист: поступление октября — 2 листа (3 − 1).
        received, _sold = collect_flows([self.m])
        self.assertEqual(received[self.m.id][(2026, 10)], D("2"))

    def test_credit_return_lowers_the_debt_only(self):
        doc = self.supply("S-2", "2026-10-03", [self.sheet_line(2, 7000)])
        line = doc["lines"][0]["id"]
        r = self.client.post(f"{SUPPLIES}{doc['id']}/return/", {
            "lines": [{"line": line, "quantity": "1"}], "mode": "CREDIT", "returned_on": "2026-10-05",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["total_cost"])), D("7000.00"))
        self.assertEqual(D(str(r.data["returned_after"])), D("3500.00"))
        self.assertEqual(D(str(r.data["debt"])), D("3500.00"))

    def test_cannot_return_twice_what_was_received(self):
        self.give_back("2")
        self.give_back("2", expect=400)
        self.give_back("1", mode="CREDIT")
        self.assertEqual(Supply.objects.get(pk=self.doc["id"]).returned_after, D("25200.00"))
        self.assertEqual(stock_value_total(), D("0.00"))

    def test_cancel_after_a_return_still_works(self):
        self.give_back()
        self.assertEqual(self.client.delete(f"{SUPPLIES}{self.doc['id']}/").status_code, 204)
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, D("0"))
        self.assertFalse(InventoryLog.objects.filter(material=self.m).exists())
        self.assertEqual(CashEntry.balance("BANK"), D("0"))
        self.assertFalse(SupplierReturn.objects.exists())

    def test_cancel_after_a_sale_is_still_refused(self):
        self.give_back()
        self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "order_date": "2026-10-09",
            "items": [{"type": "MATERIAL", "material": self.m.id, "mode": "PIECE", "quantity": "1"}],
        }, format="json")
        self.assertEqual(self.client.delete(f"{SUPPLIES}{self.doc['id']}/").status_code, 400)


class ReturnThenPriceCorrectionTests(Base):
    """«Исправить приход» после возврата открытого месяца: возврат — по новой
    цене единицы (как RS-N2 для возвратов на месте), склад и сверка сходятся."""

    def test_return_follows_the_new_unit_price(self):
        doc = self.supply("INV-77", "2026-10-02", [self.sheet_line(10, 34800)])
        r = self.client.post(f"{SUPPLIES}{doc['id']}/return/", {
            "lines": [{"line": doc["lines"][0]["id"], "quantity": "1"}], "returned_on": "2026-10-03",
            "mode": "CREDIT",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        lot = Roll.objects.get(material=self.m)
        r = self.client.post("/api/warehouse/lot-correction/apply/",
                             {"roll": lot.id, "purchase_cost": "36540"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        ret = SupplierReturn.objects.get()
        self.assertEqual(ret.amount, D("3654.00"))
        back = InventoryLog.objects.get(type=InventoryLog.Type.CORRECTION, quantity_changed__lt=0)
        self.assertEqual(back.cost, D("3654.00"))
        st = self.client.get(f"/api/warehouse/suppliers/{self.sup.id}/statement/").data
        self.assertEqual([(x["type"], D(str(x["delta"]))) for x in st["rows"]],
                         [("INVOICE", D("36540.00")), ("RETURN", D("-3654.00"))])
        self.assertEqual(D(str(st["closing_balance"])), D("32886.00"))
        self.assertEqual(stock_value_total(), D("32886.00"))
        self.assertEqual(purchases_from_stock(*OCT), D("32886.00"))
        self.assertEqual(D(str(self.report(*OCT)["stock"]["reconcile"]["gap"])), D("0.00"))
        warnings = [w["code"] for w in r.data.get("warnings", [])]
        self.assertNotIn("untracked_usage", warnings)
