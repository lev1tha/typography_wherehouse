"""ОПиУ и ОДДС по месяцам года (просьба владельца, 2026-09-27).

Главное, что держат эти тесты: ОПиУ не «второй ответ» на вопрос о прибыли —
каждый месяц в нём ровно та же прибыль, что «Сводка» (отчёт «Финансов») за этот
месяц; а ОДДС сходится с кассовой книгой: остаток на начало + поток = остаток
на конец, в том числе по каждому счёту.
"""
from datetime import date, datetime, time
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry, ExpenseEntry, ExpenseKind
from sales.models import Receipt
from sales.sale_service import create_sale
from warehouse.models import InventoryLog, Material

PNL = "/api/finance/pnl/"
CASH_FLOW = "/api/finance/cash-flow/"
REPORT = "/api/finance/report/"


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


def row(data, key):
    return next(r for r in data["rows"] if r["key"] == key)


def rows_starting(data, prefix):
    return [r for r in data["rows"] if r["key"].startswith(prefix)]


class StatementsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="st_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        # Прошлый год целиком — все месяцы уже прошли, и тест не зависит от
        # того, какое сегодня число.
        self.year = timezone.localdate().year - 1
        self.march = date(self.year, 3, 10)
        self.april = date(self.year, 4, 5)
        self.plate = Material.objects.create(
            name="Табличка", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            purchase_price=Decimal("100"), price_per_unit=Decimal("300"),
        )

    def _sale(self, day, qty=1, paid=None):
        return create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.plate,
                         "quantity": Decimal(qty), "mode": "PIECE"}],
            amount_paid=Decimal(paid) if paid is not None else None,
            created_at=noon(day),
        )

    def _expense(self, code, amount, day, account="CASH"):
        kind = ExpenseKind.objects.get(code=code)
        r = self.client.post("/api/finance/expense-entries/", {
            "kind": kind.id, "name": code, "amount": str(amount),
            "spent_at": day.isoformat(), "account": account,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def _month_report(self, month):
        first = date(self.year, month, 1)
        last = date(self.year, month + 1, 1) if month < 12 else date(self.year + 1, 1, 1)
        from datetime import timedelta
        return self.client.get(REPORT, {
            "date_from": first.isoformat(), "date_to": (last - timedelta(days=1)).isoformat(),
        }).data

    # --- ОПиУ ---------------------------------------------------------------
    def test_each_month_of_pnl_equals_the_monthly_report(self):
        self._sale(self.march, qty=3, paid="900")
        receipt = self._sale(self.march, qty=2)
        self._expense("RENT", 250, self.march)
        self._expense("CUTTER", 40, self.april)
        # Возврат одной строки в апреле — ложится на апрель.
        line = receipt.items.get()
        from sales.sale_service import refund_receipt
        refund_receipt(receipt, item_ids=[line.id], user=self.admin)
        type(line).objects.filter(pk=line.pk).update(returned_at=noon(self.april))
        # Брак в апреле.
        InventoryLog.objects.create(
            type=InventoryLog.Type.WRITE_OFF, material=self.plate,
            quantity_changed=Decimal("-1"), cost=Decimal("100"), happened_at=noon(self.april),
        )
        data = self.client.get(PNL, {"year": self.year}).data
        profit = row(data, "profit")
        for month in (3, 4, 5):
            self.assertEqual(
                Decimal(str(profit["values"][month - 1])),
                Decimal(str(self._month_report(month)["profit"])),
                f"месяц {month}",
            )
        # Март: 5 × 300 выручки − 5 × 100 себестоимости − аренда 250.
        self.assertEqual(Decimal(str(profit["values"][2])), Decimal("750"))
        # Апрель: возврат −600 выручки, +200 себестоимости, −40 расходников, −100 брака.
        self.assertEqual(Decimal(str(profit["values"][3])), Decimal("-540"))
        self.assertEqual(Decimal(str(row(data, "losses")["values"][3])), Decimal("-100"))

    def test_revenue_and_cost_lines_add_up(self):
        self._sale(self.march, qty=2)
        data = self.client.get(PNL, {"year": self.year}).data
        m = 2
        revenue = Decimal(str(row(data, "revenue")["values"][m]))
        parts = sum(
            Decimal(str(row(data, k)["values"][m]))
            for k in ("revenue_material", "revenue_cutting", "revenue_other")
        )
        self.assertEqual(revenue, Decimal("600"))
        self.assertEqual(parts, revenue)
        cogs = Decimal(str(row(data, "cogs")["values"][m]))
        self.assertEqual(
            cogs,
            Decimal(str(row(data, "cogs_material")["values"][m]))
            + Decimal(str(row(data, "cogs_services")["values"][m])),
        )

    def test_year_total_is_the_sum_of_months(self):
        self._sale(self.march)
        self._sale(self.april, qty=2)
        data = self.client.get(PNL, {"year": self.year}).data
        for r in data["rows"]:
            if r["kind"] == "percent":
                continue
            self.assertEqual(
                Decimal(str(r["total"])),
                sum((Decimal(str(v)) for v in r["values"]), Decimal("0")),
                r["key"],
            )

    def test_hidden_kind_with_money_stays_and_investments_are_not_profit(self):
        ad = ExpenseKind.objects.create(code="ad", name="Реклама", block=ExpenseKind.Block.VARIABLE)
        ExpenseEntry.objects.create(kind=ad, amount=Decimal("70"), spent_at=self.march)
        ad.is_archived = True
        ad.save()
        self._expense("EQUIPMENT", 5000, self.march)
        data = self.client.get(PNL, {"year": self.year}).data
        self.assertEqual(Decimal(str(row(data, f"kind:{ad.id}")["values"][2])), Decimal("-70"))
        self.assertEqual(Decimal(str(row(data, "investments")["values"][2])), Decimal("5000"))
        self.assertEqual(Decimal(str(row(data, "profit")["values"][2])), Decimal("-70"))

    def test_future_months_are_flagged(self):
        this_year = timezone.localdate().year
        data = self.client.get(PNL, {"year": this_year}).data
        today = timezone.localdate()
        flags = [m["future"] for m in data["months"]]
        self.assertEqual(flags, [m > today.month for m in range(1, 13)])

    # --- ОДДС ---------------------------------------------------------------
    def test_cash_flow_balances_month_by_month(self):
        self._sale(self.march, qty=2, paid="1000")          # принесли 1000 за 600 — 400 сдачей
        self._expense("RENT", 250, self.march)
        self._expense("EQUIPMENT", 300, self.april, account="BANK")
        CashEntry.objects.create(account="CASH", kind="OUT", article="OWNER_OUT",
                                 amount=Decimal("100"), happened_on=self.april)
        CashEntry.objects.create(account="BANK", kind="IN", article="LOAN_IN",
                                 amount=Decimal("2000"), happened_on=self.april)
        data = self.client.get(CASH_FLOW, {"year": self.year}).data
        opening, net, closing = row(data, "opening"), row(data, "net"), row(data, "closing")
        for i in range(12):
            self.assertEqual(
                Decimal(str(opening["values"][i])) + Decimal(str(net["values"][i])),
                Decimal(str(closing["values"][i])),
            )
            per_account = sum(
                Decimal(str(r["values"][i])) for r in rows_starting(data, "closing:")
            )
            self.assertEqual(per_account, Decimal(str(closing["values"][i])))
        last = date(self.year, 12, 31)
        self.assertEqual(Decimal(str(closing["total"])), CashEntry.balance(upto=last))

    def test_cash_flow_puts_each_movement_in_its_activity(self):
        self._sale(self.march, qty=2, paid="1000")
        self._expense("RENT", 250, self.march)
        self._expense("EQUIPMENT", 300, self.april, account="BANK")
        CashEntry.objects.create(account="CASH", kind="OUT", article="OWNER_OUT",
                                 amount=Decimal("100"), happened_on=self.april)
        data = self.client.get(CASH_FLOW, {"year": self.year}).data
        rent = ExpenseKind.objects.get(code="RENT")
        equipment = ExpenseKind.objects.get(code="EQUIPMENT")
        m, a = 2, 3
        self.assertEqual(Decimal(str(row(data, "operating:clients")["values"][m])), Decimal("1000"))
        self.assertEqual(Decimal(str(row(data, f"operating:kind:{rent.id}")["values"][m])), Decimal("-250"))
        self.assertEqual(Decimal(str(row(data, f"investing:kind:{equipment.id}")["values"][a])), Decimal("-300"))
        self.assertEqual(Decimal(str(row(data, "financing:owner_out")["values"][a])), Decimal("-100"))
        # Операционный поток марта = клиенты − аренда.
        self.assertEqual(Decimal(str(row(data, "section:operating")["values"][m])), Decimal("750"))

    def test_given_change_nets_out_of_client_money(self):
        receipt = self._sale(self.march, qty=2, paid="1000")
        from finance import cash
        cash.change_given(receipt, Decimal("400"))
        CashEntry.objects.filter(article="CHANGE").update(happened_on=self.march)
        data = self.client.get(CASH_FLOW, {"year": self.year}).data
        self.assertEqual(Decimal(str(row(data, "operating:clients")["values"][2])), Decimal("600"))

    # --- касса: расходы — только через «Финансы» ------------------------------
    def test_expense_cannot_bypass_the_pnl_through_the_cash_book(self):
        for article in ("EXPENSE", "SALARY"):
            r = self.client.post("/api/finance/cash/", {
                "account": "CASH", "kind": "OUT", "article": article,
                "amount": "10", "confirm_negative": True,
            }, format="json")
            self.assertEqual(r.status_code, 400, r.data)
            self.assertIn("Финансах", str(r.data))
        r = self.client.post("/api/finance/cash/", {
            "account": "CASH", "kind": "OUT", "article": "OWNER_OUT",
            "amount": "10", "confirm_negative": True,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)


class SupplierDebtDocumentTests(APITestCase):
    """Долг поставщику — с документом: «за что должны?» должно быть видно."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="sd2_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1470"),
        )

    def test_debt_row_carries_the_document_lines(self):
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "НК-1", "received_on": "2026-09-10", "paid_amount": "10000",
            "paid_account": "CASH",
            "lines": [{"material": self.sheet.id, "form": "SHEET",
                       "width": "1.2", "height": "2.4", "sheet_count": "10", "cost": "48000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        debts = self.client.get(REPORT).data["suppliers"]
        doc = debts["rows"][0]
        self.assertEqual(doc["label"], "Накладная НК-1")
        self.assertEqual(Decimal(str(doc["total"])), Decimal("48000"))
        self.assertEqual(Decimal(str(doc["paid"])), Decimal("10000"))
        self.assertEqual(Decimal(str(doc["debt"])), Decimal("38000"))
        self.assertEqual(doc["lines"][0]["material"], "Акрил")
        self.assertIn("1.2×2.4", doc["lines"][0]["what"])
