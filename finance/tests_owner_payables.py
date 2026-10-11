"""Решение владельца 11.10 (D-195): ПРИХОД БЕЗ УКАЗАННОЙ ОПЛАТЫ — ЭТО ДОЛГ.

Перепроверка 10.10 (docs/OWNER_RECHECK_2026-10-10.md, «Открыто после
проверки»): строка сверки «Долг поставщикам» на «Обзоре» (закуп − оплаты из
кассы) и карточка «Долг поставщикам» считали разное. Приход без способа
оплаты сверка считала долгом, а карточка — нет (на демо-данных +2 306 877
против 22 560).

Теперь:
- новый приход без способа оплаты — «в долг» (как `payment=DEBT`);
- старый приход без оплаты и без отметки долга карточка тоже считает долгом —
  расчётом (закуп партии − заплачено по кассе), данные не мигрируются; его
  оплачивают тем же путём, что LOT-строку;
- изменение долга за период в сверке = остаток на конце − остаток на начале −
  начальный долг, внесённый в периоде; остаток на сегодня = карточка «Долг
  поставщикам» минус авансы поставщикам.
"""
from datetime import date, datetime, time
from decimal import Decimal as D
from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance import cash
from finance.models import CashEntry, ExpenseEntry, ExpenseKind
from finance.reports.bridge import bridge, supplier_position
from finance.reports.overview import headline
from finance.reports.summary import supplier_advances, supplier_debts
from warehouse.models import InventoryLog, Material, Roll, Supplier
from warehouse.rolls import receive_lot
from warehouse.stock import apply_stock_change

SEP = (date(2026, 9, 1), date(2026, 9, 30))
OCT = (date(2026, 10, 1), date(2026, 10, 31))
REPORT = "/api/finance/report/"


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


class Base(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="op_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.supplier = Supplier.objects.create(name="Глобал")
        self.acrylic = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET, price_per_sqm=D("1500"),
            sheet_width=D("1.22"), sheet_height=D("2.44"), piece_area=D("2.9768"),
        )
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=D("0"), purchase_price=D("10"),
        )

    # --- помощники ----------------------------------------------------------------

    def receive(self, cost, *, payment=None, day=None, sheets="10", expect=201):
        body = {
            "material": self.acrylic.id, "form": "SHEET", "width": "1.22", "height": "2.44",
            "sheet_count": sheets, "purchase_cost": str(cost),
        }
        if payment is not None:
            body["payment"] = payment
        if day:
            body["received_on"] = day.isoformat()
        r = self.client.post("/api/warehouse/materials/receive-roll/", body, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return Roll.objects.latest("id")

    def legacy_lot(self, cost, day, sheets="10"):
        """Приход «как до 11.10»: ни оплаты, ни отметки «в долг»."""
        return receive_lot(
            self.acrylic, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"),
            sheet_count=D(sheets), purchase_cost=D(cost), user=self.admin, received_at=noon(day),
        )

    def pay_lot(self, lot, expect=200, **body):
        r = self.client.post(f"/api/warehouse/rolls/{lot.id}/pay-supplier/", body, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data

    def legacy_material_expense(self, amount, day):
        entry = ExpenseEntry.objects.create(
            kind=ExpenseKind.objects.get(code=ExpenseKind.MATERIAL_PURCHASE), amount=D(amount),
            spent_at=day, account="CASH", created_by=self.admin,
        )
        cash.sync_expense(entry, user=self.admin)
        self.assertTrue(CashEntry.objects.filter(expense=entry).exists())
        return entry

    def card_net(self):
        return supplier_debts()["total"] - supplier_advances()["total"]

    def lot_row(self, lot):
        return next((r for r in supplier_debts()["rows"] if r["kind"] == "LOT" and r["id"] == lot.id), None)


class NewIntakeIsDebtTests(Base):
    """Новый приход без способа оплаты — «в долг»."""

    def test_receive_roll_without_payment_is_a_debt(self):
        lot = self.receive(48000)
        self.assertEqual(lot.supplier_debt, D("48000.00"))
        self.assertEqual(CashEntry.balance(), D("0"))
        self.assertEqual(supplier_debts()["total"], D("48000.00"))

    def test_blank_payment_is_a_debt_too(self):
        lot = self.receive(48000, payment="")
        self.assertEqual(lot.supplier_debt, D("48000.00"))

    def test_quick_piece_intake_without_payment_is_a_debt(self):
        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": self.bolts.id, "quantity": "100", "actual_price": "12",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        lot = Roll.objects.get(material=self.bolts)
        self.assertEqual(lot.supplier_debt, D("1200.00"))
        self.assertEqual(supplier_debts()["total"], D("1200.00"))

    def test_cash_and_bank_are_chosen_explicitly(self):
        lot = self.receive(48000, payment="CASH")
        self.assertEqual(lot.supplier_debt, D("0"))
        self.assertEqual(CashEntry.balance("CASH"), D("-48000"))
        self.assertEqual(supplier_debts()["total"], D("0"))


class LegacyLotTests(Base):
    """Старый приход без оплаты и без отметки долга — долг расчётом."""

    def test_card_counts_it_as_a_debt(self):
        lot = self.legacy_lot("30000", date(2026, 9, 5))
        self.assertEqual(lot.supplier_debt, D("0"))          # данные не трогаем
        row = self.lot_row(lot)
        self.assertIsNotNone(row, supplier_debts()["rows"])
        self.assertEqual(row["debt"], D("30000"))
        self.assertTrue(row["unmarked"])
        self.assertEqual(supplier_debts()["total"], D("30000"))

    def test_paying_it_closes_exactly_that_lot(self):
        lot = self.legacy_lot("30000", date(2026, 9, 5))
        other = self.legacy_lot("12000", date(2026, 9, 6), sheets="4")
        data = self.pay_lot(lot, amount="10000", account="BANK", paid_on="2026-10-02")
        self.assertEqual(D(str(data["supplier_debt"])), D("20000.00"))
        self.assertEqual(self.lot_row(lot)["debt"], D("20000"))
        self.assertFalse(self.lot_row(lot)["unmarked"])
        self.assertEqual(self.lot_row(other)["debt"], D("12000"), "другой приход не тронут")
        self.assertEqual(CashEntry.balance("BANK"), D("-10000"))
        # Пустая сумма — весь остаток именно этой партии.
        self.pay_lot(lot, account="CASH", paid_on="2026-10-03")
        self.assertIsNone(self.lot_row(lot))
        self.assertEqual(CashEntry.balance("CASH"), D("-20000"))
        self.assertEqual(supplier_debts()["total"], D("12000"))
        self.assertEqual(bridge(None, None)["unexplained"], D("0"))

    def test_cannot_overpay_it(self):
        lot = self.legacy_lot("30000", date(2026, 9, 5))
        self.pay_lot(lot, expect=400, amount="30001", account="CASH")
        self.assertEqual(CashEntry.balance(), D("0"))

    def test_lot_from_an_invoice_is_paid_by_the_invoice(self):
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "ТН-1", "supplier": self.supplier.id, "received_on": "2026-10-02",
            "lines": [{"material": self.acrylic.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                       "sheet_count": "3", "cost": "9000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        lot = Roll.objects.get(supply_line__supply_id=r.data["id"])
        self.assertIsNone(self.lot_row(lot))
        self.pay_lot(lot, expect=400, amount="100", account="CASH")
        self.assertEqual(supplier_debts()["total"], D("9000"))

    def test_lot_paid_on_intake_is_not_a_debt(self):
        receive_lot(self.acrylic, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"),
                    sheet_count=D("2"), purchase_cost=D("6000"), received_at=noon(date(2026, 9, 7)),
                    paid_account="CASH")
        self.assertEqual(supplier_debts()["total"], D("0"))


class CardEqualsBridgeTests(Base):
    """Изменение долга в сверке = разница остатка карточки на концах периода."""

    def test_overview_for_all_time_change_equals_the_card(self):
        self.legacy_lot("30000", date(2026, 9, 5))
        credit = self.receive(20000, payment="DEBT", day=date(2026, 9, 10))
        self.pay_lot(credit, amount="5000", account="CASH", paid_on="2026-10-03")
        self.receive(10000, payment="CASH", day=date(2026, 10, 4))
        self.assertEqual(supplier_debts()["total"], D("45000"))
        h = headline(None, None)
        row = next(r for r in h["why"]["reasons"] if r["key"] == "payables")
        self.assertEqual(row["amount"], D("45000"))
        self.assertEqual(row["balance"], D("45000"))
        self.assertEqual(row["balance_end"], D("45000"))
        self.assertEqual(row["balance_start"], D("0"))
        self.assertEqual(h["why"]["unexplained"], D("0"))

    def scenario(self):
        """Все виды прихода и оплаты за сентябрь–октябрь."""
        self.legacy = self.legacy_lot("30000", date(2026, 9, 5))
        self.credit = self.receive(20000, payment="DEBT", day=date(2026, 9, 10))
        # 5 листов 1,22 × 2,44 = 14,884 кв.м за 12 000: цена кв.м 806,24 даёт
        # в журнале 12 000,08 — закуп считается суммой партии, без хвоста.
        self.tail = self.legacy_lot("12000", date(2026, 9, 12), sheets="5")
        # Приход без партии (старый быстрый приход, крепёж демо-данных).
        apply_stock_change(self.bolts, D("500"), log_type=InventoryLog.Type.SUPPLY,
                           actual_price=D("10"), reason="Поступление", happened_at=noon(date(2026, 9, 15)))
        r = self.client.post("/api/warehouse/supplier-opening-debts/", {
            "supplier": self.supplier.id, "amount": "7000", "as_of": "2026-09-20",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "ТН-1", "supplier": self.supplier.id, "received_on": "2026-10-02",
            "paid_amount": "8000", "paid_account": "CASH",
            "lines": [{"material": self.acrylic.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                       "sheet_count": "16", "cost": "48000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.supply_id = r.data["id"]
        self.pay_lot(self.credit, amount="5000", account="CASH", paid_on="2026-10-03")
        r = self.client.post("/api/warehouse/supplier-payments/", {
            "supplier": self.supplier.id, "amount": "2000", "account": "BANK", "paid_on": "2026-10-04",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        # Старая ручная трата «Закуп материала» (новые API не принимает) —
        # деньги поставщику без документа.
        self.legacy_material_expense("1500", date(2026, 10, 5))
        self.receive(9000, payment="CASH", day=date(2026, 10, 6), sheets="3")
        r = self.client.post(f"/api/warehouse/supplies/{self.supply_id}/pay/",
                             {"amount": "10000", "account": "BANK", "paid_on": "2026-10-07"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_card_rows(self):
        self.scenario()
        rows = supplier_debts()["rows"]
        by = {(r["kind"], r["id"]): r["debt"] for r in rows}
        self.assertEqual(by[("LOT", self.legacy.id)], D("30000"))
        self.assertEqual(by[("LOT", self.credit.id)], D("15000"))
        self.assertEqual(by[("LOT", self.tail.id)], D("12000"))
        self.assertEqual(by[("SUPPLY", self.supply_id)], D("30000"))
        log = next(r for r in rows if r["kind"] == "LOG")
        self.assertEqual((log["total"], log["paid"], log["debt"]), (D("5000.00"), D("1500"), D("3500.00")))
        self.assertFalse(log["payable"])
        # Начальный долг 7 000 − аванс 2 000 — строкой поставщика.
        ledger = next(r for r in rows if r["kind"] == "LEDGER")
        self.assertEqual(ledger["debt"], D("5000"))
        self.assertEqual(supplier_debts()["total"], D("95500.00"))
        self.assertEqual(supplier_advances()["total"], D("0"))

    def test_whole_period_and_each_month_match_the_card(self):
        self.scenario()
        whole = bridge(None, None)
        lines = {line["key"]: line["amount"] for line in whole["lines"]}
        self.assertEqual(whole["unexplained"], D("0"))
        # 124 000 закуплено − 35 500 заплачено; карточка 95 500 = это + начальный долг 7 000.
        self.assertEqual(lines["payables"], D("88500.00"))
        self.assertEqual(self.card_net(), D("95500.00"))
        self.assertEqual(whole["levels"]["payables_end"], self.card_net())
        self.assertEqual(
            lines["payables"],
            whole["levels"]["payables_end"] - whole["levels"]["payables_start"]
            - whole["levels"]["payables_opening"],
        )
        self.assertEqual(supplier_position(timezone.localdate()), self.card_net())
        total = D("0")
        for first, last in (SEP, OCT):
            b = bridge(first, last)
            amount = next(line["amount"] for line in b["lines"] if line["key"] == "payables")
            levels = b["levels"]
            self.assertEqual(b["unexplained"], D("0"), first)
            self.assertEqual(
                amount, levels["payables_end"] - levels["payables_start"] - levels["payables_opening"], first,
            )
            total += amount
        self.assertEqual(total, lines["payables"])
        self.assertEqual(supplier_position(SEP[1]), D("74000.00"))  # 30+20+12+5 + начальный 7

    def test_overview_reason_shows_both_ends(self):
        self.scenario()
        h = headline(*OCT)
        row = next(r for r in h["why"]["reasons"] if r["key"] == "payables")
        self.assertEqual(row["balance_start"], D("74000.00"))
        self.assertEqual(row["balance_end"], D("95500.00"))
        self.assertEqual(row["opening"], D("0"))
        self.assertEqual(row["amount"], D("21500.00"))
        self.assertEqual(row["start_on"], SEP[1])

    def test_supplier_payment_without_document_beyond_the_log_is_an_advance(self):
        apply_stock_change(self.bolts, D("100"), log_type=InventoryLog.Type.SUPPLY,
                           actual_price=D("10"), happened_at=noon(date(2026, 9, 15)))
        self.legacy_material_expense("1500", date(2026, 10, 5))
        self.assertFalse([r for r in supplier_debts()["rows"] if r["kind"] == "LOG"])
        adv = supplier_advances()
        self.assertEqual(adv["total"], D("500.00"))
        self.assertEqual(adv["rows"][0]["kind"], "UNLINKED")
        lines = {line["key"]: line["amount"] for line in bridge(None, None)["lines"]}
        self.assertEqual(lines["payables"], self.card_net())

    def test_foreign_payment_at_a_lower_rate(self):
        """Курсовой доход закрывает долг без денег: сверка двигает долг на всю
        закрытую сумму, как карточка, а не только на деньги из кассы."""
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "USD-1", "supplier": self.supplier.id, "received_on": "2026-10-02",
            "currency": "USD", "rate": "87.5",
            "lines": [{"material": self.acrylic.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                       "sheet_count": "3", "cost": "0", "cost_fc": "1000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.post(f"/api/warehouse/supplies/{r.data['id']}/pay/",
                             {"amount": "1000", "rate": "86", "account": "BANK", "paid_on": "2026-10-06"},
                             format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.card_net(), D("0"))
        b = bridge(*OCT)
        lines = {line["key"]: line["amount"] for line in b["lines"]}
        self.assertEqual(lines["payables"], D("0"))
        self.assertEqual(lines["accrued"], D("0"))
        self.assertEqual(b["unexplained"], D("0"))

    def test_overpaid_lot_is_money_at_the_supplier(self):
        lot = self.receive(10000, payment="CASH", day=date(2026, 10, 2), sheets="4")
        r = self.client.post("/api/warehouse/lot-correction/apply/",
                             {"roll": lot.id, "purchase_cost": "8000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(supplier_debts()["total"], D("0"))
        adv = supplier_advances()
        self.assertEqual(adv["total"], D("2000.00"))
        lines = {line["key"]: line["amount"] for line in bridge(None, None)["lines"]}
        self.assertEqual(lines["payables"], D("-2000.00"))
        self.assertEqual(lines["payables"], self.card_net())


class CommandTests(Base):
    def run_cmd(self):
        out = StringIO()
        call_command("lots_without_payment", stdout=out)
        return out.getvalue()

    def test_lists_only_lots_without_payment_and_mark(self):
        self.legacy_lot("30000", date(2026, 9, 5))
        self.receive(20000, payment="DEBT", day=date(2026, 9, 10))
        self.receive(9000, payment="CASH", day=date(2026, 9, 11), sheets="3")
        apply_stock_change(self.bolts, D("500"), log_type=InventoryLog.Type.SUPPLY,
                           actual_price=D("10"), happened_at=noon(date(2026, 9, 15)))
        out = self.run_cmd()
        self.assertIn("05.09.2026", out)
        self.assertIn("Акрил", out)
        self.assertIn("30 000", out)
        self.assertNotIn("20 000", out)
        self.assertNotIn("9 000", out)
        self.assertIn("Крепёж", out)          # приход без партии — отдельным списком
        self.assertIn("5 000", out)
        self.assertIn("35 000", out)          # итого

    def test_nothing_to_list(self):
        self.receive(20000, payment="DEBT")
        self.assertIn("нет", self.run_cmd())
