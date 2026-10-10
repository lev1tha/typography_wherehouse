"""Входящие остатки клиентов при переезде из Excel (XL-04/F6/CLI-06, волна 2).

Сценарий владельца: на дату переезда пять клиентов должны по 12 000. Это не
выручка, не налог, не прибыль и не касса, но долг: карточка, список должников,
возраст долга, плитка «Долг», акт сверки. Оплата 12 000 — касса +12 000, долг
48 000, сверка ОПиУ→ОДДС — «Не объяснено» 0 (строка «Входящие остатки»).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from audit.models import AuditLog
from clients.models import Client, ClientAdvance, OpeningBalance
from clients.opening import parse_number, parse_text
from clients.testkit import D, ShopCase
from finance.models import CashEntry, ExpenseEntry
from finance.reports import bridge as bridge_mod
from finance.reports.pnl import pnl
from finance.reports.summary import finance_summary

PHONES = ["+996555000001", "0555 000 002", "555000003", "+996 (555) 00-00-04", "996555000005"]


def cash_total():
    total = Decimal("0")
    for kind, amount in CashEntry.objects.values_list("kind", "amount"):
        total += amount if kind == "IN" else -amount
    return total


class ParseTests(ShopCase):
    def test_numbers_with_spaces_and_comma(self):
        self.assertEqual(parse_number("12 000"), D("12000.00"))
        self.assertEqual(parse_number("12 000,50"), D("12000.50"))
        self.assertEqual(parse_number("1,234,567"), D("1234567.00"))
        self.assertEqual(parse_number("12.000,5"), D("12000.50"))
        self.assertEqual(parse_number("-5 000"), D("-5000.00"))
        self.assertEqual(parse_number("7 500 сом"), D("7500.00"))
        self.assertIsNone(parse_number(""))
        for bad in ("abc", "1,234", "12-00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_number(bad)

    def test_rows_tabs_semicolons_header_and_swapped_columns(self):
        rows = parse_text(
            "Телефон\tИмя\tДолг\n"
            "0555 11 22 33\tТахир\t12 000\n"
            "Айбек;0700 123 456;-3 000,50\n"
            "0777 000 111\tОсОО «Ак Жол»\t5 000\t1 000\n"
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[0]["phone"], rows[0]["debt"], rows[0]["advance"]), ("0555 11 22 33", D("12000.00"), None))
        self.assertEqual((rows[1]["phone"], rows[1]["name"], rows[1]["advance"]), ("0700 123 456", "Айбек", D("3000.50")))
        self.assertEqual((rows[2]["debt"], rows[2]["advance"]), (D("5000.00"), D("1000.00")))


class OpeningCase(ShopCase):
    AS_OF = date(2026, 10, 1)

    def paste(self, lines):
        return "\n".join(lines)

    def five_debts(self):
        text = self.paste(f"{p}\tКлиент {i}\t12 000" for i, p in enumerate(PHONES, start=1))
        r = self.client.post("/api/clients/opening-balances/",
                             {"text": text, "as_of": self.AS_OF.isoformat()}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data


class PreviewAndPostTests(OpeningCase):
    def test_preview_finds_and_creates(self):
        Client.objects.create(full_name="Клиент 1", phone="0555 000 001")
        text = (
            "+996 555 000 001\tКлиент 1\t12 000\n"
            "0555 000 009\tНовый\t-2 000\n"
            "12\tКороткий\t100\n"
            "0555 000 009\tПовтор\t300\n"
            "0555 000 010\tНоль\t0\n"
        )
        r = self.client.post("/api/clients/opening-balances/preview/", {"text": text}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        st = [row["status"] for row in r.data["rows"]]
        self.assertEqual(st, ["found", "create", "error", "error", "error"])
        self.assertEqual(r.data["totals"]["debt"], "12000.00")
        self.assertEqual(r.data["totals"]["advance"], "2000.00")
        # Провести с ошибками нельзя — ничего не создаётся.
        r = self.client.post("/api/clients/opening-balances/",
                             {"text": text, "as_of": "2026-10-01"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(OpeningBalance.objects.exists())

    def test_double_import_is_an_error(self):
        self.five_debts()
        r = self.client.post("/api/clients/opening-balances/preview/",
                             {"text": f"{PHONES[0]}\tКлиент 1\t1 000"}, format="json")
        self.assertEqual(r.data["rows"][0]["status"], "error")

    def test_roles(self):
        self.client.force_authenticate(self.store)
        r = self.client.post("/api/clients/opening-balances/",
                             {"text": f"{PHONES[0]}\tX\t100", "as_of": "2026-10-01"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.client.get("/api/clients/opening-balances/").status_code, 200)
        r = self.client.post("/api/clients/opening-balances/preview/", {"text": "x"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_future_date_rejected(self):
        future = (timezone.localdate() + timedelta(days=3)).isoformat()
        r = self.client.post("/api/clients/opening-balances/",
                             {"text": f"{PHONES[0]}\tX\t100", "as_of": future}, format="json")
        self.assertEqual(r.status_code, 400)


class OwnerScenarioTests(OpeningCase):
    def test_five_debts_then_payment(self):
        cash_before = cash_total()
        data = self.five_debts()
        self.assertEqual(data["created_clients"], 5)
        self.assertEqual(Decimal(data["debt"]), D("60000"))
        self.assertTrue(AuditLog.objects.filter(action__startswith="Проведены входящие остатки").exists())

        # Не выручка, не налог, не прибыль, не касса.
        month = (date(2026, 10, 1), date(2026, 10, 31))
        p = pnl(*month)
        self.assertEqual((p["revenue"], p["tax"], p["net_profit"]), (D("0"), D("0"), D("0")))
        self.assertEqual(cash_total(), cash_before)

        # Долг клиентов 60 000: список должников, плитка «Долг», корзины возраста.
        r = self.client.get("/api/clients/clients/", {"has_debt": 1, "page_size": 50})
        self.assertEqual(r.data["count"], 5)
        self.assertEqual(sum(Decimal(str(c["debt"])) for c in r.data["results"]), D("60000"))
        self.assertEqual(finance_summary(*month)["client_debt"], D("60000"))
        aging = self.client.get("/api/clients/clients/aging/").data
        self.assertEqual(Decimal(aging["total"]), D("60000"))
        self.assertEqual(Decimal(aging["opening"]), D("60000"))

        # Оплата 12 000 первым клиентом — касса +12 000, долг 48 000.
        first = Client.objects.get(phone=PHONES[0])
        r = self.client.post(f"/api/clients/clients/{first.id}/pay-debt/",
                             {"amount": "12000", "method": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["debt"])), D("0"))
        self.assertEqual(cash_total() - cash_before, D("12000"))
        r = self.client.get("/api/clients/clients/", {"has_debt": 1, "page_size": 50})
        self.assertEqual(sum(Decimal(str(c["debt"])) for c in r.data["results"]), D("48000"))
        self.assertEqual(finance_summary(*month)["client_debt"], D("48000"))

        # Сверка: «Не объяснено» 0, деньги — строкой «Входящие остатки».
        b = bridge_mod.bridge(*month)
        self.assertEqual(b["unexplained"], D("0"))
        line = {x["key"]: x["amount"] for x in b["lines"]}
        self.assertEqual(line["opening_balances"], D("12000"))
        self.assertEqual(b["net_cash_flow"], D("12000"))

    def test_card_statement_and_age(self):
        self.five_debts()
        c = Client.objects.get(phone=PHONES[1])
        card = self.card(c)
        self.assertEqual(Decimal(str(card["debt"])), D("12000"))
        self.assertEqual(Decimal(str(card["balance"])), D("12000"))
        self.assertEqual(card["oldest_debt_at"], self.AS_OF.isoformat())
        self.assertEqual(len(card["opening_balances"]), 1)
        # Акт сверки: на период после переезда — входящее сальдо 12 000.
        st = self.statement(c, date_from="2026-10-02")
        self.assertEqual(Decimal(st["opening"]), D("12000"))
        st = self.statement(c)
        self.assertEqual([r["kind"] for r in st["rows"]], ["opening_debt"])
        self.assertEqual(Decimal(st["closing"]), D("12000"))
        # Фильтр «должен не меньше N дней» видит входящий долг.
        days = (timezone.localdate() - self.AS_OF).days
        r = self.client.get("/api/clients/clients/", {"overdue_days": days, "page_size": 50})
        self.assertEqual(r.data["count"], 5)

    def test_pay_debt_takes_opening_first_then_orders(self):
        self.five_debts()
        c = Client.objects.get(phone=PHONES[2])
        order = self.sale(5000, client=c)                                    # заказ в долг
        r = self.client.post(f"/api/clients/clients/{c.id}/pay-debt/",
                             {"amount": "14000", "method": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        sources = [(a["source"], Decimal(str(a["amount"]))) for a in r.data["allocations"]]
        self.assertEqual(sources, [("opening", D("12000")), ("cash", D("2000"))])
        order.refresh_from_db()
        self.assertEqual(order.debt, D("3000"))
        self.assertEqual(Decimal(str(r.data["debt"])), D("3000"))
        st = self.statement(c)
        self.assertEqual(Decimal(st["closing"]), D("3000"))
        self.assertIn("opening_payment", [x["kind"] for x in st["rows"]])

    def test_selected_opening_only_and_overpay(self):
        self.five_debts()
        c = Client.objects.get(phone=PHONES[3])
        ob = OpeningBalance.objects.get(client=c)
        r = self.client.post(f"/api/clients/clients/{c.id}/pay-debt/",
                             {"amount": "13000", "receipt_ids": [f"opening:{ob.id}"]}, format="json")
        self.assertEqual(r.status_code, 400)                                 # лишнее некуда деть
        ob.refresh_from_db()
        self.assertEqual(ob.remaining, D("12000"))
        r = self.client.post(f"/api/clients/clients/{c.id}/pay-debt/",
                             {"amount": "5000", "receipt_ids": [f"opening:{ob.id}"]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        ob.refresh_from_db()
        self.assertEqual(ob.remaining, D("7000"))

    def test_write_off_and_revert(self):
        self.five_debts()
        c = Client.objects.get(phone=PHONES[4])
        cash_before = cash_total()
        r = self.client.post(f"/api/clients/clients/{c.id}/pay-debt/",
                             {"method": "WRITE_OFF", "note": "уехал"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(cash_total(), cash_before)
        self.assertTrue(ExpenseEntry.objects.filter(kind__code="BAD_DEBT", amount=D("12000")).exists())
        month = (date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(bridge_mod.bridge(*month)["unexplained"], D("0"))
        # Отменить можно только нетронутый остаток.
        ob = OpeningBalance.objects.get(client=c)
        self.assertEqual(self.client.post(f"/api/clients/opening-balances/{ob.id}/revert/").status_code, 400)
        other = OpeningBalance.objects.get(client__phone=PHONES[0])
        r = self.client.post(f"/api/clients/opening-balances/{other.id}/revert/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(self.card(other.client)["debt"])), D("0"))

    def test_storekeeper_cannot_write_off_opening(self):
        self.five_debts()
        c = Client.objects.get(phone=PHONES[0])
        self.client.force_authenticate(self.store)
        r = self.client.post(f"/api/clients/clients/{c.id}/pay-debt/", {"method": "WRITE_OFF"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(OpeningBalance.objects.get(client=c).remaining, D("12000"))


class OpeningAdvanceTests(OpeningCase):
    def test_opening_advance_has_no_cash_and_is_spent(self):
        r = self.client.post("/api/clients/opening-balances/",
                             {"text": f"{self.agency.phone}\tАк Жол\t-5 000", "as_of": "2026-10-01"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertFalse(CashEntry.objects.exists())
        adv = ClientAdvance.objects.get()
        self.assertTrue(adv.is_opening)
        self.assertEqual(D(str(self.card()["advance_balance"])), D("5000"))
        # Тратим в заказе на 3 000: касса не двигается, сверка — ноль.
        body = {"client_id": self.agency.id, "payment_method": "CASH", "use_advance": True,
                "items": [{"type": "MATERIAL", "material": self.coin.id, "quantity": "3000", "mode": "PIECE"}]}
        r = self.client.post("/api/sales/receipts/checkout/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["debt"])), D("0"))
        self.assertFalse(CashEntry.objects.exists())
        today = timezone.localdate()
        b = bridge_mod.bridge(today.replace(day=1), today)
        self.assertEqual(b["unexplained"], D("0"))
        line = {x["key"]: x["amount"] for x in b["lines"]}
        self.assertEqual(line["opening_balances"], D("-3000"))
        self.assertEqual(b["levels"]["receivables"], D("0"))
        st = self.statement()
        self.assertEqual(Decimal(st["closing"]), D("-2000"))
        self.assertIn("opening_advance", [x["kind"] for x in st["rows"]])

    def test_revert_opening_advance_without_cash(self):
        self.client.post("/api/clients/opening-balances/",
                         {"text": f"{self.agency.phone}\tАк Жол\t-5 000", "as_of": "2026-10-01"}, format="json")
        ob = OpeningBalance.objects.get()
        r = self.client.post(f"/api/clients/opening-balances/{ob.id}/revert/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(CashEntry.objects.exists())
        self.assertEqual(D(str(self.card()["advance_balance"])), D("0"))
        self.assertEqual(self.statement()["rows"], [])


class MergeTests(OpeningCase):
    def test_opening_moves_with_merge(self):
        self.five_debts()
        drop = Client.objects.get(phone=PHONES[0])
        r = self.client.post(f"/api/clients/clients/{self.agency.id}/merge/", {"from": drop.id}, format="json")
        self.assertIn(r.status_code, (200, 201), getattr(r, "data", r))
        self.assertEqual(D(str(self.card()["debt"])), D("12000"))
        self.assertEqual(OpeningBalance.objects.get(client=self.agency).amount, D("12000"))


class CheckoutPaysOpeningTests(OpeningCase):
    def test_order_plus_opening_debt_at_checkout(self):
        self.five_debts()
        c = Client.objects.get(phone=PHONES[0])
        cash_before = cash_total()
        body = {"client_id": c.id, "payment_method": "CASH", "pay_debt": True, "amount_paid": "15000",
                "items": [{"type": "MATERIAL", "material": self.coin.id, "quantity": "3000", "mode": "PIECE"}]}
        r = self.client.post("/api/sales/receipts/checkout/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["debt"])), D("0"))
        self.assertEqual(D(str(r.data["change_due"])), D("0"))           # не осело сдачей
        self.assertEqual(D(str(r.data["debt_paid"])), D("12000"))
        self.assertEqual(OpeningBalance.objects.get(client=c).remaining, D("0"))
        self.assertEqual(cash_total() - cash_before, D("15000"))
        today = timezone.localdate()
        self.assertEqual(bridge_mod.bridge(today.replace(day=1), today)["unexplained"], D("0"))
