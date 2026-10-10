"""Поставщики и накладные — волна 1 аудита «владелец против Excel» (10.10).

F1 (оплата без счёта), cash-06/07 (платежи строками, аванс, сальдо), G1-N2
(возврат поставщику), G1-N3 (валюта и курсовая разница), STK-01 (штучные
партии), F11 (дубли), STK-09 (начальные остатки), STAFF-07 (закуп скрыт от
складовщика), XL-07 (журнал «было → стало»), подсказки и PATCH с `lines`.
"""
from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from finance.models import CashEntry, ExpenseEntry, ExpenseKind, PeriodLock
from finance.reports.bridge import bridge
from finance.reports.summary import supplier_debts
from finance.material_sheet import purchases_from_stock
from warehouse.models import (
    InventoryLog,
    Material,
    Roll,
    Supplier,
    SupplierPayment,
    Supply,
)
from warehouse.rolls import consume_area

D = Decimal
CASH, BANK = "CASH", "BANK"
SUPPLIES = "/api/warehouse/supplies/"
PAYMENTS = "/api/warehouse/supplier-payments/"
SUPPLIERS = "/api/warehouse/suppliers/"
MONTH = (date(2026, 10, 1), date(2026, 10, 31))


class Base(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="w1_admin", password="x", role=User.Role.ADMIN)
        self.keeper = User.objects.create_user(username="w1_keeper", password="x", role=User.Role.STOREKEEPER)
        self.accountant = User.objects.create_user(username="w1_acc", password="x", role=User.Role.ACCOUNTANT)
        self.client.force_authenticate(self.admin)
        self.supplier = Supplier.objects.create(name="Глобал")
        self.sheet = Material.objects.create(
            name="Акрил 2мм", unit=Material.Unit.SQM, is_roll_material=True,
            piece_area=D("2.9768"), price_per_sqm=D("1470"),
            sheet_width=D("1.22"), sheet_height=D("2.44"),
        )
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=D("0"), purchase_price=D("0"),
        )

    def sheet_line(self, cost="25200", count="3", **extra):
        return {"material": self.sheet.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                "sheet_count": count, "cost": cost, **extra}

    def bolts_line(self, qty="100", cost="1200", **extra):
        return {"material": self.bolts.id, "form": "QTY", "quantity": qty, "cost": cost, **extra}

    def post_supply(self, lines=None, expect=201, **over):
        data = {"number": "ТН-1", "supplier": self.supplier.id, "received_on": "2026-10-05",
                "lines": lines or [self.sheet_line()], **over}
        r = self.client.post(SUPPLIES, data, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def pay(self, supply_id, expect=200, **body):
        r = self.client.post(f"{SUPPLIES}{supply_id}/pay/", body, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def unexplained(self):
        return bridge(*MONTH)["unexplained"]


# --- F1: оплата поставщику без счёта ----------------------------------------------


class F1UnpaidCashTests(Base):
    def test_paid_without_account_is_refused(self):
        r = self.client.post(SUPPLIES, {
            "received_on": "2026-10-05", "paid_amount": "5000", "lines": [self.bolts_line()],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("paid_account", r.data)
        self.assertFalse(Supply.objects.exists())
        self.assertFalse(CashEntry.objects.exists())

    def test_paid_with_account_writes_the_cash(self):
        self.post_supply([self.bolts_line()], paid_amount="5000", paid_account=BANK)
        self.assertEqual(CashEntry.balance(BANK), D("-5000"))

    def test_zero_paid_needs_no_account(self):
        self.post_supply([self.bolts_line()], paid_amount="0")
        self.assertFalse(CashEntry.objects.exists())

    def test_command_lists_then_fixes_missing_cash_entries(self):
        # Накладная, проведённая ДО исправления: «оплачено» есть, записи в кассе нет.
        broken = Supply.objects.create(
            number="СТ-7", supplier=self.supplier, received_on=date(2026, 10, 3),
            paid_amount=D("68000"), paid_account="",
        )
        fine = Supply.objects.create(
            number="ОК-1", supplier=self.supplier, received_on=date(2026, 10, 3),
            paid_amount=D("1000"), paid_account=CASH,
        )
        CashEntry.objects.create(
            kind=CashEntry.Kind.OUT, article=CashEntry.Article.SUPPLY, amount=D("1000"),
            account=CASH, supply=fine, happened_on=date(2026, 10, 3), is_auto=True,
        )
        out = StringIO()
        call_command("supplies_unpaid_cash", stdout=out)
        text = out.getvalue()
        self.assertIn("СТ-7", text)
        self.assertNotIn("ОК-1", text)
        self.assertIn("68000", text.replace(" ", ""))
        self.assertEqual(CashEntry.objects.count(), 1, "по умолчанию команда только показывает")

        call_command("supplies_unpaid_cash", "--fix", "--account", "BANK",
                     "--ids", str(broken.id), stdout=StringIO())
        entry = CashEntry.objects.get(supply=broken)
        self.assertEqual(entry.account, BANK)
        self.assertEqual(entry.amount, D("68000"))
        self.assertEqual(entry.kind, CashEntry.Kind.OUT)
        self.assertEqual(entry.happened_on, date(2026, 10, 3))
        broken.refresh_from_db()
        self.assertEqual(broken.paid_account, BANK)
        self.assertTrue(AuditLog.objects.filter(action__contains="СТ-7").exists())
        # Повторный запуск ничего не находит и ничего не удваивает.
        out = StringIO()
        call_command("supplies_unpaid_cash", "--fix", "--account", "BANK",
                     "--ids", str(broken.id), stdout=out)
        self.assertEqual(CashEntry.objects.filter(supply=broken).count(), 1)


# --- cash-06 / cash-07: платежи строками, аванс, сальдо -----------------------------


class PaymentRowsTests(Base):
    def test_two_accounts_two_rows_and_the_cash_matches(self):
        supply = self.post_supply([self.sheet_line("48000", "10")])
        self.pay(supply["id"], amount="30000", account=CASH, paid_on="2026-10-06")
        data = self.pay(supply["id"], amount="10000", account=BANK, paid_on="2026-10-07")
        self.assertEqual(CashEntry.balance(CASH), D("-30000"))
        self.assertEqual(CashEntry.balance(BANK), D("-10000"))
        self.assertEqual(D(str(data["debt"])), D("8000"))
        self.assertEqual(D(str(data["paid_total"])), D("40000"))
        self.assertEqual(D(str(data["paid_amount"])), D("0"), "старое поле не трогаем")
        self.assertEqual(len(data["payments"]), 2)
        self.assertEqual(SupplierPayment.objects.count(), 2)

    def test_legacy_field_and_rows_add_up(self):
        supply = self.post_supply([self.sheet_line("48000", "10")], paid_amount="20000", paid_account=CASH)
        data = self.pay(supply["id"], amount="10000", account=BANK)
        self.assertEqual(D(str(data["paid_total"])), D("30000"))
        self.assertEqual(D(str(data["debt"])), D("18000"))

    def test_editing_one_row_moves_only_its_amount(self):
        supply = self.post_supply([self.sheet_line("48000", "10")])
        self.pay(supply["id"], amount="30000", account=CASH, paid_on="2026-10-06")
        self.pay(supply["id"], amount="10000", account=CASH, paid_on="2026-10-07")
        second = SupplierPayment.objects.order_by("id").last()
        r = self.client.patch(f"{PAYMENTS}{second.id}/", {"account": BANK}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(CASH), D("-30000"))
        self.assertEqual(CashEntry.balance(BANK), D("-10000"))
        self.assertEqual(Supply.objects.get(pk=supply["id"]).debt, D("8000"))
        self.assertTrue(AuditLog.objects.filter(action__contains="счёт").exists())

    def test_deleting_a_row_returns_the_money_and_the_debt(self):
        supply = self.post_supply([self.sheet_line("48000", "10")])
        self.pay(supply["id"], amount="30000", account=CASH)
        row = SupplierPayment.objects.get()
        self.assertEqual(self.client.delete(f"{PAYMENTS}{row.id}/").status_code, 204)
        self.assertEqual(CashEntry.balance(CASH), D("0"))
        self.assertEqual(Supply.objects.get(pk=supply["id"]).debt, D("48000"))

    def test_cannot_pay_more_than_the_debt(self):
        supply = self.post_supply([self.bolts_line()])
        self.pay(supply["id"], expect=400, amount="1201", account=CASH)

    def test_advance_without_invoice_and_offset(self):
        r = self.client.post(PAYMENTS, {"supplier": self.supplier.id, "amount": "5000", "account": BANK,
                                         "paid_on": "2026-10-04"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        advance = SupplierPayment.objects.get()
        self.assertIsNone(advance.supply_id)
        self.assertEqual(CashEntry.balance(BANK), D("-5000"))
        # Аванс виден в сальдо: деньги лежат у поставщика.
        row = next(s for s in self.client.get(SUPPLIERS).data if s["id"] == self.supplier.id)
        self.assertEqual(D(str(row["balance"]["saldo"])), D("-5000"))
        self.assertEqual(D(str(row["balance"]["credit"])), D("5000"))
        # Пришла накладная на 1200 — зачитываем аванс.
        supply = self.post_supply([self.bolts_line()])
        r = self.client.post(f"{PAYMENTS}{advance.id}/apply/",
                             {"supply": supply["id"], "amount": "1200"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        fresh = self.client.get(f"{SUPPLIES}{supply['id']}/").data
        self.assertEqual(D(str(fresh["debt"])), D("0"))
        self.assertEqual(CashEntry.balance(BANK), D("-5000"), "зачёт денег не двигает")
        # Сальдо: накладная 1200 − аванс 5000 = −3800; зачёт сальдо не меняет.
        row = next(s for s in self.client.get(SUPPLIERS).data if s["id"] == self.supplier.id)
        self.assertEqual(D(str(row["balance"]["saldo"])), D("-3800"))
        self.assertEqual(D(str(row["balance"]["advances"])), D("3800"))
        # Больше остатка аванса не зачесть.
        other = self.post_supply([self.bolts_line("10", "9000")], number="ТН-2")
        r = self.client.post(f"{PAYMENTS}{advance.id}/apply/",
                             {"supply": other["id"], "amount": "9000"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_supplier_balance_is_invoices_minus_payments(self):
        s1 = self.post_supply([self.bolts_line("10", "1000")], number="А")
        self.post_supply([self.bolts_line("10", "2000")], number="Б")
        self.pay(s1["id"], amount="400", account=CASH)
        self.client.post(f"{SUPPLIERS}{self.supplier.id}/opening-debt/", {}, format="json")
        r = self.client.post("/api/warehouse/supplier-opening-debts/", {
            "supplier": self.supplier.id, "amount": "7000", "as_of": "2026-09-30", "note": "долг на переезд",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        st = self.client.get(f"{SUPPLIERS}{self.supplier.id}/statement/").data
        # 7000 + 1000 + 2000 − 400
        self.assertEqual(D(str(st["balance"]["saldo"])), D("9600"))
        self.assertEqual(D(str(st["closing_balance"])), D("9600"))
        kinds = [row["type"] for row in st["rows"]]
        self.assertEqual(kinds.count("OPENING"), 1)
        self.assertEqual(kinds.count("INVOICE"), 2)
        self.assertEqual(kinds.count("PAYMENT"), 1)
        self.assertEqual(D(str(st["rows"][0]["balance"])), D("7000"), "входящий долг идёт первым")

    def test_statement_csv(self):
        s1 = self.post_supply([self.bolts_line("10", "1000")], number="А")
        self.pay(s1["id"], amount="400", account=CASH)
        r = self.client.get(f"{SUPPLIERS}{self.supplier.id}/statement/?export=csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        text = r.content.decode("utf-8-sig")
        self.assertIn("Накладная А", text)
        self.assertIn("Оплата", text)
        self.assertIn(";", text)
        self.assertEqual(self.client.get(PAYMENTS + "?export=csv").status_code, 200)

    def test_debt_report_uses_the_same_formula(self):
        supply = self.post_supply([self.sheet_line("48000", "10")])
        self.pay(supply["id"], amount="30000", account=CASH)
        report = supplier_debts()
        self.assertEqual(report["total"], D("18000"))
        rows = self.client.get(SUPPLIES + "?unpaid=1").data
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([r["id"] for r in rows], [supply["id"]])

    def test_bridge_stays_at_zero(self):
        supply = self.post_supply([self.sheet_line("48000", "10")])
        self.pay(supply["id"], amount="30000", account=CASH, paid_on="2026-10-06")
        self.client.post(PAYMENTS, {"supplier": self.supplier.id, "amount": "2000", "account": BANK,
                                    "paid_on": "2026-10-08"}, format="json")
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_cancelling_the_invoice_returns_all_its_money(self):
        supply = self.post_supply([self.sheet_line("48000", "10")], paid_amount="8000", paid_account=CASH)
        self.pay(supply["id"], amount="30000", account=BANK)
        self.assertEqual(self.client.delete(f"{SUPPLIES}{supply['id']}/").status_code, 204)
        self.assertEqual(CashEntry.balance(CASH), D("0"))
        self.assertEqual(CashEntry.balance(BANK), D("0"))
        self.assertFalse(SupplierPayment.objects.exists())

    def test_payment_in_a_closed_period_is_refused(self):
        supply = self.post_supply([self.bolts_line()])
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 10)
        lock.save()
        self.pay(supply["id"], expect=400, amount="100", account=CASH, paid_on="2026-10-06")

    def test_accountant_reads_but_does_not_write(self):
        supply = self.post_supply([self.bolts_line()])
        self.pay(supply["id"], amount="100", account=CASH)
        self.client.force_authenticate(self.accountant)
        self.assertEqual(self.client.get(PAYMENTS).status_code, 200)
        self.assertEqual(
            self.client.get(f"{SUPPLIERS}{self.supplier.id}/statement/").status_code, 200)
        r = self.client.post(PAYMENTS, {"supplier": self.supplier.id, "amount": "1", "account": CASH},
                             format="json")
        self.assertEqual(r.status_code, 403)
        self.client.force_authenticate(self.keeper)
        self.assertEqual(self.client.get(PAYMENTS).status_code, 403)


# --- G1-N2: возврат поставщику ------------------------------------------------------------


class SupplierReturnTests(Base):
    def setUp(self):
        super().setUp()
        self.supply = self.post_supply([self.sheet_line("25200", "3")], paid_amount="25200",
                                       paid_account=BANK)

    def _return(self, expect=200, **over):
        line = Supply.objects.get(pk=self.supply["id"]).lines.get()
        body = {"lines": [{"line": line.id, "quantity": "1"}], "mode": "REFUND", "account": BANK,
                "returned_on": "2026-10-08", **over}
        r = self.client.post(f"{SUPPLIES}{self.supply['id']}/return/", body, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def test_refund_back_to_the_account(self):
        data = self._return()
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, D("5.9536"))  # 2 листа по 2.9768 кв.м
        self.assertEqual(D(str(data["total_cost"])), D("16800.00"))
        self.assertEqual(D(str(data["debt"])), D("0"))
        self.assertEqual(CashEntry.balance(BANK), D("-16800"), "8 400 вернулись")
        roll = Roll.objects.get(material=self.sheet)
        self.assertEqual(roll.purchase_cost, D("16800.00"))
        self.assertEqual(roll.sheet_count, D("2"))
        self.assertEqual(roll.remaining_area, roll.initial_area)

    def test_no_loss_in_the_pnl_and_purchases_drop(self):
        self._return()
        from finance.reports.pnl import pnl

        p = pnl(*MONTH)
        self.assertEqual(p["cogs_total"], D("0"), "возврат поставщику — не себестоимость и не потеря")
        self.assertFalse(InventoryLog.objects.filter(type=InventoryLog.Type.WRITE_OFF).exists())
        self.assertEqual(purchases_from_stock(*MONTH), D("16800.00"))
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_credit_keeps_the_money_at_the_supplier(self):
        data = self._return(mode="CREDIT")
        self.assertEqual(CashEntry.balance(BANK), D("-25200"), "деньги остались у поставщика")
        self.assertEqual(D(str(data["overpaid"])), D("8400.00"))
        row = next(s for s in self.client.get(SUPPLIERS).data if s["id"] == self.supplier.id)
        self.assertEqual(D(str(row["balance"]["saldo"])), D("-8400"))
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_unpaid_invoice_just_gets_smaller(self):
        other = self.post_supply([self.bolts_line("100", "1000")], number="ТН-2")
        line = Supply.objects.get(pk=other["id"]).lines.get()
        r = self.client.post(f"{SUPPLIES}{other['id']}/return/", {
            "lines": [{"line": line.id, "quantity": "40"}], "mode": "CREDIT",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["total_cost"])), D("600.00"))
        self.assertEqual(D(str(r.data["debt"])), D("600.00"))
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("60"))
        # Деньгами вернуть нечем: по накладной ничего не оплачено.
        r = self.client.post(f"{SUPPLIES}{other['id']}/return/", {
            "lines": [{"line": line.id, "quantity": "10"}], "mode": "REFUND", "account": CASH,
        }, format="json")
        self.assertEqual(r.status_code, 400)

    def test_cannot_return_what_is_already_sold(self):
        consume_area(self.sheet, D("2") * D("2.9768"))   # ушло 2 листа из 3
        self._return(expect=400, lines=[{"line": Supply.objects.get(pk=self.supply["id"]).lines.get().id,
                                         "quantity": "2"}])
        self._return()                                   # а один — можно

    def test_journal_and_audit_trail(self):
        self._return()
        self.assertTrue(InventoryLog.objects.filter(
            type=InventoryLog.Type.CORRECTION, reason__startswith="Возврат поставщику").exists())
        self.assertTrue(AuditLog.objects.filter(action__startswith="Возврат поставщику").exists())
        st = self.client.get(f"{SUPPLIERS}{self.supplier.id}/statement/").data
        kinds = [r["type"] for r in st["rows"]]
        self.assertIn("RETURN", kinds)
        self.assertIn("REFUND", kinds)
        self.assertEqual(D(str(st["closing_balance"])), D("0"))

    def test_return_from_a_closed_month_is_refused(self):
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 31)
        lock.save()
        self._return(expect=400)

    def test_cancel_after_a_return_still_works(self):
        self._return()
        self.assertEqual(self.client.delete(f"{SUPPLIES}{self.supply['id']}/").status_code, 204)
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, D("0"))
        self.assertEqual(CashEntry.balance(BANK), D("0"))

    def test_whole_line_back(self):
        line = Supply.objects.get(pk=self.supply["id"]).lines.get()
        r = self.client.post(f"{SUPPLIES}{self.supply['id']}/return/", {
            "lines": [{"line": line.id, "quantity": "3"}], "mode": "REFUND", "account": CASH,
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["total_cost"])), D("0"))
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, D("0"))
        self.assertEqual(CashEntry.balance(CASH), D("25200"))


# --- G1-N3: валюта ----------------------------------------------------------------------


class ForeignCurrencyTests(Base):
    def usd_supply(self, **over):
        return self.post_supply(
            [self.sheet_line("0", "3", cost_fc="1000")], currency="USD", rate="87.5",
            stated_total="1000", **over,
        )

    def test_cost_in_som_is_taken_at_the_supply_rate(self):
        data = self.usd_supply()
        self.assertEqual(D(str(data["total_cost"])), D("87500.00"))
        self.assertEqual(D(str(data["total_foreign"])), D("1000.00"))
        self.assertEqual(D(str(data["debt_foreign"])), D("1000.00"))
        self.assertEqual(D(str(data["debt"])), D("87500.00"))
        self.assertEqual(D(str(data["discrepancy"])), D("0.00"))
        roll = Roll.objects.get(material=self.sheet)
        self.assertEqual(roll.purchase_cost, D("87500.00"), "партия — в сомах по курсу накладной")
        self.assertEqual(purchases_from_stock(*MONTH), D("87500.00"))

    def test_rate_is_required_for_a_foreign_supply(self):
        r = self.client.post(SUPPLIES, {
            "received_on": "2026-10-05", "currency": "USD",
            "lines": [self.sheet_line("0", "3", cost_fc="1000")],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.post(SUPPLIES, {
            "received_on": "2026-10-05", "currency": "USD", "rate": "87.5",
            "lines": [self.sheet_line("0", "3")],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_pay_at_a_higher_rate_books_a_loss(self):
        supply = self.usd_supply()
        data = self.pay(supply["id"], amount="1000", rate="89.1", account=BANK, paid_on="2026-10-10")
        self.assertEqual(D(str(data["debt"])), D("0"))
        self.assertEqual(D(str(data["debt_foreign"])), D("0"))
        # С банка ушло 89 100: 87 500 — долг, 1 600 — курсовая разница.
        self.assertEqual(CashEntry.balance(BANK), D("-89100"))
        fx = ExpenseEntry.objects.get(kind__code="FX_DIFF")
        self.assertEqual(fx.amount, D("1600"))
        row = SupplierPayment.objects.get()
        self.assertEqual((row.amount, row.fx_diff, row.amount_fc, row.rate),
                         (D("89100"), D("1600"), D("1000"), D("89.1")))
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_pay_at_a_lower_rate_books_an_income(self):
        supply = self.usd_supply()
        self.pay(supply["id"], amount="1000", rate="86", account=BANK, paid_on="2026-10-10")
        self.assertEqual(CashEntry.balance(BANK), D("-86000"))
        fx = ExpenseEntry.objects.get(kind__code="FX_DIFF")
        self.assertEqual(fx.amount, D("-1500"))
        self.assertEqual(Supply.objects.get(pk=supply["id"]).debt, D("0"))
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_partial_payments_leave_the_remainder_in_both_currencies(self):
        supply = self.usd_supply()
        data = self.pay(supply["id"], amount="400", rate="88", account=CASH, paid_on="2026-10-08")
        self.assertEqual(D(str(data["debt_foreign"])), D("600.00"))
        self.assertEqual(D(str(data["debt"])), D("52500.00"))
        data = self.pay(supply["id"], amount="600", rate="90", account=CASH, paid_on="2026-10-10")
        self.assertEqual(D(str(data["debt"])), D("0"))
        self.assertEqual(CashEntry.balance(CASH), D("-89200"))   # 400 × 88 + 600 × 90
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_pay_needs_the_rate_and_not_more_than_the_debt(self):
        supply = self.usd_supply()
        self.pay(supply["id"], expect=400, amount="100", account=CASH)
        self.pay(supply["id"], expect=400, amount="1001", rate="88", account=CASH)

    def test_deleting_a_foreign_payment_reverses_the_difference(self):
        supply = self.usd_supply()
        self.pay(supply["id"], amount="1000", rate="89.1", account=BANK, paid_on="2026-10-10")
        row = SupplierPayment.objects.get()
        self.assertEqual(self.client.delete(f"{PAYMENTS}{row.id}/").status_code, 204)
        self.assertEqual(CashEntry.balance(BANK), D("0"))
        self.assertEqual(sum(e.amount for e in ExpenseEntry.objects.filter(kind__code="FX_DIFF")), D("0"))
        self.assertEqual(Supply.objects.get(pk=supply["id"]).debt, D("87500"))

    def test_supplier_card_shows_the_foreign_debt(self):
        self.usd_supply()
        row = next(s for s in self.client.get(SUPPLIERS).data if s["id"] == self.supplier.id)
        self.assertEqual(row["balance"]["foreign_debts"], {"USD": "1000.00"})
        self.assertEqual(D(str(row["balance"]["saldo"])), D("87500"))


# --- STK-01: штучные партии ---------------------------------------------------------------


class PieceLotTests(Base):
    def test_piece_line_creates_a_lot(self):
        data = self.post_supply([self.bolts_line("100", "1200")])
        roll = Roll.objects.get(material=self.bolts)
        self.assertEqual(roll.form, Roll.Form.PIECE)
        self.assertEqual(roll.initial_area, D("100"))
        self.assertEqual(roll.purchase_cost, D("1200"))
        self.assertEqual(roll.cost_per_sqm, D("12"))
        line = Supply.objects.get(pk=data["id"]).lines.get()
        self.assertEqual(line.roll_id, roll.id)
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("100"))
        self.assertEqual(self.bolts.purchase_price, D("12"))

    def test_sale_takes_lots_first_then_the_old_loose_stock(self):
        # Старый остаток без партии: 50 шт по 10 (цена из карточки).
        self.bolts.quantity, self.bolts.purchase_price = D("50"), D("10")
        self.bolts.save()
        self.post_supply([self.bolts_line("100", "2000")])   # партия по 20
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("150"))
        # Продажа 120 шт: сначала партия (100 × 20), потом 20 из «без партии» по
        # закупочной карточки (последняя цена 20 — как и сейчас в системе).
        cost = consume_area(self.bolts, D("120"))
        self.assertEqual(cost, D("100") * D("20") + D("20") * D("20"))
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("30"))

    def test_cancel_removes_the_piece_lot(self):
        data = self.post_supply([self.bolts_line("100", "1200")])
        self.assertEqual(self.client.delete(f"{SUPPLIES}{data['id']}/").status_code, 204)
        self.assertFalse(Roll.objects.filter(material=self.bolts).exists())
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("0"))

    def test_cancel_is_refused_once_the_lot_was_used(self):
        data = self.post_supply([self.bolts_line("100", "1200")])
        consume_area(self.bolts, D("10"))
        self.assertEqual(self.client.delete(f"{SUPPLIES}{data['id']}/").status_code, 400)

    def test_purchase_in_the_report_is_the_line_sum(self):
        self.post_supply([self.bolts_line("100", "1200")])
        self.assertEqual(purchases_from_stock(*MONTH), D("1200"))
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_lot_correction_works_on_a_piece_line(self):
        data = self.post_supply([self.bolts_line("100", "1200")])
        line = Supply.objects.get(pk=data["id"]).lines.get()
        r = self.client.post("/api/warehouse/lot-correction/apply/",
                             {"supply_line": line.id, "purchase_cost": "1500"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Supply.objects.get(pk=data["id"]).total_cost, D("1500"))
        self.assertEqual(Roll.objects.get(material=self.bolts).purchase_cost, D("1500"))


# --- F11: двойной ввод --------------------------------------------------------------------


class DuplicateTests(Base):
    def test_same_supplier_number_and_date_is_a_conflict(self):
        first = self.post_supply([self.bolts_line()])
        r = self.client.post(SUPPLIES, {
            "number": "ТН-1", "supplier": self.supplier.id, "received_on": "2026-10-05",
            "lines": [self.bolts_line("5", "50")],
        }, format="json")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["code"], "duplicate_supply")
        self.assertEqual(r.data["duplicate"]["id"], first["id"])
        self.assertEqual(Supply.objects.count(), 1)

    def test_without_number_supplier_amount_and_date_decide(self):
        self.post_supply([self.bolts_line("100", "1200")], number="")
        r = self.client.post(SUPPLIES, {
            "supplier": self.supplier.id, "received_on": "2026-10-05",
            "lines": [self.bolts_line("10", "1200")],
        }, format="json")
        self.assertEqual(r.status_code, 409, r.data)
        # Другая сумма — другая поставка.
        r = self.client.post(SUPPLIES, {
            "supplier": self.supplier.id, "received_on": "2026-10-05",
            "lines": [self.bolts_line("10", "1300")],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_force_goes_through_and_is_logged(self):
        self.post_supply([self.bolts_line()])
        r = self.client.post(SUPPLIES, {
            "number": "ТН-1", "supplier": self.supplier.id, "received_on": "2026-10-05",
            "lines": [self.bolts_line("5", "50")], "force": True,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Supply.objects.count(), 2)
        self.assertTrue(AuditLog.objects.filter(action__contains="повторно").exists())

    def test_list_marks_possible_duplicates(self):
        a = self.post_supply([self.bolts_line()])
        self.client.post(SUPPLIES, {
            "number": "ТН-1", "supplier": self.supplier.id, "received_on": "2026-10-05",
            "lines": [self.bolts_line("5", "50")], "force": True,
        }, format="json")
        solo = self.post_supply([self.bolts_line()], number="ТН-9")
        rows = self.client.get(SUPPLIES).data
        rows = rows["results"] if isinstance(rows, dict) else rows
        by_id = {r["id"]: r for r in rows}
        self.assertIsNotNone(by_id[a["id"]]["possible_duplicate_of"])
        self.assertIsNone(by_id[solo["id"]]["possible_duplicate_of"])


# --- STK-09: начальные остатки ------------------------------------------------------------


class OpeningStockTests(Base):
    def test_opening_supply_makes_lots_but_no_purchase_debt_or_cash(self):
        data = self.post_supply([self.sheet_line("25200", "3")], is_opening=True, number="Остаток")
        self.assertTrue(data["is_opening"])
        roll = Roll.objects.get(material=self.sheet)
        self.assertEqual(roll.purchase_cost, D("25200"))
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, roll.initial_area)
        self.assertEqual(purchases_from_stock(*MONTH), D("0"))
        self.assertEqual(D(str(data["debt"])), D("0"))
        self.assertFalse(CashEntry.objects.exists())
        self.assertEqual(supplier_debts()["total"], D("0"))
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_opening_supply_refuses_payment(self):
        r = self.client.post(SUPPLIES, {
            "received_on": "2026-10-01", "is_opening": True, "paid_amount": "100", "paid_account": CASH,
            "lines": [self.bolts_line()],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_opening_stock_is_sold_at_its_cost(self):
        self.post_supply([self.sheet_line("25200", "3")], is_opening=True)
        cost = consume_area(self.sheet, D("2.9768"))
        self.assertEqual(cost.quantize(D("1")), D("8400"))

    def test_opening_debt_enters_the_balance_only(self):
        r = self.client.post("/api/warehouse/supplier-opening-debts/", {
            "supplier": self.supplier.id, "amount": "68000", "as_of": "2026-10-01",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        row = next(s for s in self.client.get(SUPPLIERS).data if s["id"] == self.supplier.id)
        self.assertEqual(D(str(row["balance"]["saldo"])), D("68000"))
        self.assertEqual(purchases_from_stock(*MONTH), D("0"))
        self.assertFalse(CashEntry.objects.exists())
        self.assertEqual(self.unexplained(), D("0.00"))
        # Оплата начального долга — платёж-аванс на поставщика.
        r = self.client.post(PAYMENTS, {"supplier": self.supplier.id, "amount": "8000", "account": CASH,
                                         "paid_on": "2026-10-09"}, format="json")
        self.assertEqual(r.status_code, 201)
        row = next(s for s in self.client.get(SUPPLIERS).data if s["id"] == self.supplier.id)
        self.assertEqual(D(str(row["balance"]["saldo"])), D("60000"))
        self.assertEqual(self.unexplained(), D("0.00"))


# --- STAFF-07, XL-07, прочее -------------------------------------------------------------------


class MoneyHiddenFromStorekeeperTests(Base):
    def test_storekeeper_sees_no_prices(self):
        data = self.post_supply([self.bolts_line()])
        self.client.force_authenticate(self.keeper)
        for payload in (
            self.client.get(f"{SUPPLIES}{data['id']}/").data,
            self.client.get(SUPPLIES).data[0] if isinstance(self.client.get(SUPPLIES).data, list)
            else self.client.get(SUPPLIES).data["results"][0],
        ):
            for key in ("total_cost", "debt", "paid_total", "stated_total", "paid_amount",
                        "discrepancy", "payments", "returns"):
                self.assertIsNone(payload[key], key)
            line = payload["lines"][0]
            self.assertIsNone(line["cost"])
            self.assertIsNone(line["unit_cost"])
            self.assertEqual(line["material_name"], "Крепёж")
        row = next(s for s in self.client.get(SUPPLIERS).data if s["id"] == self.supplier.id)
        self.assertIsNone(row["debt"])
        self.assertIsNone(row["balance"])
        self.assertEqual(self.client.get(f"{SUPPLIERS}{self.supplier.id}/statement/").status_code, 403)

    def test_storekeeper_can_still_create_a_supply(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.post(SUPPLIES, {"received_on": "2026-10-05", "lines": [self.bolts_line()]},
                             format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(r.data["total_cost"])
        self.assertEqual(Supply.objects.get().total_cost, D("1200"))

    def test_admin_sees_everything(self):
        data = self.post_supply([self.bolts_line()])
        self.assertEqual(D(str(data["total_cost"])), D("1200"))
        self.assertEqual(D(str(data["lines"][0]["cost"])), D("1200"))


class JournalTests(Base):
    def test_patch_is_logged_with_before_and_after(self):
        data = self.post_supply([self.bolts_line()], number="ТН-1", note="старое")
        r = self.client.patch(f"{SUPPLIES}{data['id']}/", {"number": "ТН-2", "note": "новое"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        entry = AuditLog.objects.filter(action__startswith="Накладная ТН-2 изменена").get()
        self.assertIn("номер: ТН-1 → ТН-2", entry.action)
        self.assertIn("примечание: старое → новое", entry.action)

    def test_payment_events_are_logged(self):
        data = self.post_supply([self.bolts_line()])
        self.pay(data["id"], amount="200", account=CASH)
        row = SupplierPayment.objects.get()
        self.client.patch(f"{PAYMENTS}{row.id}/", {"account": BANK}, format="json")
        self.client.delete(f"{PAYMENTS}{row.id}/")
        texts = list(AuditLog.objects.values_list("action", flat=True))
        self.assertTrue(any(t.startswith("Оплата по накладной") for t in texts))
        self.assertTrue(any("→" in t and "Платёж поставщику изменён" in t for t in texts))
        self.assertTrue(any(t.startswith("Платёж поставщику удалён") for t in texts))

    def test_cancel_hint_points_to_the_new_tools(self):
        data = self.post_supply([self.sheet_line("25200", "3")])
        consume_area(self.sheet, D("1"))
        r = self.client.delete(f"{SUPPLIES}{data['id']}/")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Исправить приход", r.data["detail"])
        self.assertNotIn("инвентаризац", r.data["detail"])


class FxInThePnlTests(Base):
    """Курсовая разница доходит до ОПиУ: расход уменьшает прибыль, доход — увеличивает."""

    def _pnl(self):
        from finance.reports.pnl import pnl

        return pnl(*MONTH)

    def _opex(self):
        return self._pnl()["opex"]["total"]

    def test_loss_and_income_go_into_the_pnl(self):
        base = self._opex()
        supply = self.post_supply(
            [self.sheet_line("0", "3", cost_fc="1000")], currency="USD", rate="87.5",
        )
        self.pay(supply["id"], amount="400", rate="90", account=BANK, paid_on="2026-10-10")
        self.assertEqual(self._opex() - base, D("1000"))      # 400 × (90 − 87.5)
        self.pay(supply["id"], amount="600", rate="85", account=BANK, paid_on="2026-10-10")
        self.assertEqual(self._opex() - base, D("-500"))      # + 600 × (85 − 87.5) = −1 500
        self.assertEqual(self.unexplained(), D("0.00"))

    def test_correction_of_a_foreign_line_keeps_both_currencies_in_step(self):
        supply = self.post_supply(
            [self.sheet_line("0", "3", cost_fc="1000")], currency="USD", rate="87.5",
        )
        line = Supply.objects.get(pk=supply["id"]).lines.get()
        r = self.client.post("/api/warehouse/lot-correction/apply/",
                             {"supply_line": line.id, "purchase_cost": "96250"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        line.refresh_from_db()
        self.assertEqual(line.cost, D("96250.00"))
        self.assertEqual(line.cost_fc, D("1100.00"))


class PaymentEditEdgeTests(Base):
    def test_date_change_moves_the_cash_row_and_respects_the_lock(self):
        supply = self.post_supply([self.bolts_line()])
        self.pay(supply["id"], amount="200", account=CASH, paid_on="2026-10-06")
        row = SupplierPayment.objects.get()
        r = self.client.patch(f"{PAYMENTS}{row.id}/", {"paid_on": "2026-10-07"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.objects.get(supply_id=supply["id"]).happened_on, date(2026, 10, 7))
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 7)
        lock.save()
        r = self.client.patch(f"{PAYMENTS}{row.id}/", {"account": BANK}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_foreign_payment_is_not_edited_in_place(self):
        supply = self.post_supply([self.sheet_line("0", "3", cost_fc="1000")], currency="USD", rate="87.5")
        self.pay(supply["id"], amount="100", rate="90", account=CASH, paid_on="2026-10-08")
        row = SupplierPayment.objects.get()
        r = self.client.patch(f"{PAYMENTS}{row.id}/", {"account": BANK}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.patch(f"{PAYMENTS}{row.id}/", {"note": "уточнили"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_advance_with_offsets_cannot_be_deleted(self):
        self.client.post(PAYMENTS, {"supplier": self.supplier.id, "amount": "500", "account": CASH,
                                    "paid_on": "2026-10-04"}, format="json")
        advance = SupplierPayment.objects.get()
        supply = self.post_supply([self.bolts_line()])
        self.client.post(f"{PAYMENTS}{advance.id}/apply/", {"supply": supply["id"], "amount": "300"},
                         format="json")
        self.assertEqual(self.client.delete(f"{PAYMENTS}{advance.id}/").status_code, 400)
        offset = SupplierPayment.objects.get(kind=SupplierPayment.Kind.OFFSET)
        self.assertEqual(self.client.delete(f"{PAYMENTS}{offset.id}/").status_code, 204)
        self.assertEqual(CashEntry.balance(CASH), D("-500"), "зачёт денег не двигал")
        self.assertEqual(self.client.delete(f"{PAYMENTS}{advance.id}/").status_code, 204)
        self.assertEqual(CashEntry.balance(CASH), D("0"))

    def test_deleting_a_refund_takes_the_money_back_out(self):
        supply = self.post_supply([self.sheet_line("25200", "3")], paid_amount="25200", paid_account=BANK)
        line = Supply.objects.get(pk=supply["id"]).lines.get()
        self.client.post(f"{SUPPLIES}{supply['id']}/return/", {
            "lines": [{"line": line.id, "quantity": "1"}], "mode": "REFUND", "account": BANK,
        }, format="json")
        refund = SupplierPayment.objects.get(kind=SupplierPayment.Kind.REFUND)
        self.assertEqual(CashEntry.balance(BANK), D("-16800"))
        self.assertEqual(self.client.delete(f"{PAYMENTS}{refund.id}/").status_code, 204)
        self.assertEqual(CashEntry.balance(BANK), D("-25200"))

    def test_advance_needs_a_supplier(self):
        r = self.client.post(PAYMENTS, {"amount": "10", "account": CASH}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_summary_debt_nets_opening_debt_and_advances(self):
        self.post_supply([self.bolts_line("10", "1000")], number="А")
        self.client.post("/api/warehouse/supplier-opening-debts/", {
            "supplier": self.supplier.id, "amount": "4000", "as_of": "2026-09-30"}, format="json")
        report = supplier_debts()
        self.assertEqual(report["total"], D("5000"))
        self.assertEqual({r["kind"] for r in report["rows"]}, {"SUPPLY", "LEDGER"})
        # Аванс 3 000 без накладной закрывает часть общего долга.
        self.client.post(PAYMENTS, {"supplier": self.supplier.id, "amount": "3000", "account": CASH,
                                    "paid_on": "2026-10-06"}, format="json")
        self.assertEqual(supplier_debts()["total"], D("2000"))
        self.assertEqual(self.unexplained(), D("0.00"))
