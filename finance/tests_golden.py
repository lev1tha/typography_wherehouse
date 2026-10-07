"""Золотой набор: квартал операций с итогами, посчитанными вручную (этап 4).

Октябрь–декабрь 2026, налог 4 % от выручки (ставка с 10.2026, D-19). Один
материал «Табличка» — продаётся по 300, себестоимость 100 (штучный без партий,
себестоимость по закупочной цене). Все суммы ниже посчитаны руками; тест
сверяет ОПиУ, ОДДС, остатки по счетам и сверку до тыйына.

ОПЕРАЦИИ
---------------------------------------------------------------------------
Сентябрь
  коммуналка за сентябрь 3 456,78 — оплачена 15.10 наличными («за какой месяц» = 09)
Октябрь
  01.10  ввод начального остатка: касса 50 000,00                (вне потока)
  01.10  займ получен на банк 150 000,00                          (финансовая)
  03.10  S1: 6 шт. + 4 шт. = 3 000, оплачено наличными целиком   (себест. 1 000)
  05.10  S2: 5 шт. = 1 500, принесли 500 наличными, долг 1 000   (себест. 500)
  07.10  O1: онлайн 2 шт. = 600, оплата подтверждена → банк 600  (себест. 200)
  07.10  O2: онлайн 1 шт. = 300, НЕ оплачен — ни выручки, ни долга
  10.10  аренда октября 25 000,00 наличными
  15.10  коммуналка сентября 3 456,78 наличными
  20.10  приход «Крепёж» 37 шт. × 12,37 = 457,69, оплачен наличными
  25.10  станок 120 000,00 с банка (актив, 60 мес. → 2 000,00 в мес. с ноября)
  28.10  перевод касса → банк 10 000,00 (обе половины)
  31.10  пересчёт кассы: недостача 13,50
Ноябрь
  02.11  S2 погасил долг 1 000 наличными
  05.11  S3: 7 шт. = 2 100, принесли 2 500 наличными → сдача 400 у нас (себест. 700)
  09.11  S4: 4 шт. = 1 200, MBank                                (себест. 400)
  10.11  компьютер для ЧПУ 24 000,00 с банка (актив, 400,00 в мес. с декабря)
  12.11  приход «Крепёж» 20 шт. × 15,55 = 311,00 в долг
  14.11  брак 2 шт. «Таблички» — потери 200
  20.11  проценты по займу 1 234,56 с банка
  25.11  владелец забрал 5 000,00 из кассы
  28.11  перевод касса → банк 3 000,00 — внесена только половина «из кассы»
  30.11  возврат строки S1 (4 шт. = 1 200) наличными            (себест. −400)
  аренда ноября 25 000,00 — оплачена 03.12 с банка («за какой месяц» = 11)
Декабрь
  01.12  вторая половина перевода: банк + 3 000,00
  04.12  аренда декабря 25 000,00 с банка
  10.12  S6: 150 шт. = 45 000, DemirBank                         (себест. 15 000)
  12.12  дрель 8 500,00 наличными — дешевле порога, расход месяца
  15.12  уплачен налог за октябрь и ноябрь 288,00 наличными (204 + 84)
  компьютер сломался: «амортизировать до» 12.2026 → 400 + списание 23 600
  20.12  пересчёт кассы: излишек 7,25
  22.12  касса, статья «Прочее»: приход 150,00
  27.12  погашено тело займа 10 000,00 с банка

ОПиУ (руками)
---------------------------------------------------------------------------
                        Октябрь       Ноябрь        Декабрь       Квартал
Выручка                 5 100,00      2 100,00      45 000,00     52 200,00
  (ноябрь: 2 100 + 1 200 − 1 200 возврат S1)
Себестоимость           1 700,00        700,00      15 000,00     17 400,00
Потери                      0,00        200,00           0,00        200,00
Валовая прибыль         3 400,00      1 200,00      30 000,00     34 600,00
Операционные расходы   25 000,00     25 000,00      33 500,00     83 500,00
  (декабрь: аренда 25 000 + дрель 8 500)
Недостача/излишек         −13,50          0,00           7,25         −6,25
EBITDA                −21 613,50    −23 800,00      −3 492,75    −48 906,25
Амортизация                 0,00      2 000,00       2 400,00      4 400,00
Списание выбывшего          0,00          0,00      23 600,00     23 600,00
Операционная прибыль  −21 613,50    −25 800,00     −29 492,75    −76 906,25
Проценты                    0,00      1 234,56           0,00      1 234,56
Налог 4 %                 204,00         84,00       1 800,00      2 088,00
Чистая прибыль        −21 817,50    −27 118,56     −31 292,75    −80 228,81

ОДДС (руками)
---------------------------------------------------------------------------
                        Октябрь       Ноябрь        Декабрь
Остаток на начало           0,00     55 172,03      25 437,47
Операционная          −24 827,97      2 265,44     −13 630,75
  клиенты (оплаты−сдача) 4 100,00      4 700,00      45 000,00
  возвраты                  0,00     −1 200,00           0,00
  поставщики             −457,69          0,00           0,00
  аренда              −25 000,00          0,00     −50 000,00
  коммуналка           −3 456,78          0,00           0,00
  дрель (дешевле порога)    0,00          0,00      −8 500,00
  проценты                  0,00     −1 234,56           0,00
  налог уплаченный          0,00          0,00        −288,00
  пересчёт кассы          −13,50          0,00           7,25
  прочее                    0,00          0,00         150,00
Инвестиционная       −120 000,00    −24 000,00           0,00
Финансовая            150 000,00     −5 000,00     −10 000,00
Чистый поток            5 172,03    −26 734,56     −23 630,75
Вне потока             50 000,00     −3 000,00       3 000,00
  (переводы не сведены:     0,00     −3 000,00       3 000,00)
Остаток на конец       55 172,03     25 437,47       4 806,72
  в т.ч. касса         14 572,03      8 872,03         241,28
  в т.ч. банк          40 600,00     16 565,44       4 565,44

СВЕРКА (руками; знак — влияние на деньги)
---------------------------------------------------------------------------
                        Октябрь       Ноябрь        Декабрь
Чистая прибыль        −21 817,50    −27 118,56     −31 292,75
Амортизация и списание      0,00      2 000,00      26 000,00
Долг клиентов          −1 000,00      1 000,00           0,00
Сдача и предоплаты          0,00        400,00           0,00
Запасы на складе        1 242,31        589,00      15 000,00
Долг поставщикам            0,00        311,00           0,00
Расходы не оплачены    −3 456,78     25 000,00     −25 000,00
Налог не уплачен          204,00         84,00       1 512,00
Покупка оборудования −120 000,00    −24 000,00           0,00
Владелец и займы      150 000,00     −5 000,00     −10 000,00
Прочее                      0,00          0,00         150,00
Не объяснено                0,00          0,00           0,00
= Чистый поток          5 172,03    −26 734,56     −23 630,75
"""
from datetime import date, datetime, time
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry, ExpenseEntry, ExpenseKind
from finance.reports import depreciation
from finance.reports.bridge import bridge
from finance.reports.cashflow import cash_flow, cash_flow_year
from finance.reports.daily import daily_report
from finance.reports.overview import headline
from finance.reports.pnl import pnl, pnl_year
from finance.reports.summary import finance_summary
from sales import sale_service
from sales.models import Receipt
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot

D = Decimal
OCT = (date(2026, 10, 1), date(2026, 10, 31))
NOV = (date(2026, 11, 1), date(2026, 11, 30))
DEC = (date(2026, 12, 1), date(2026, 12, 31))
QUARTER = (date(2026, 10, 1), date(2026, 12, 31))
MONTHS = {"oct": OCT, "nov": NOV, "dec": DEC}


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


# --- Ожидаемые итоги, посчитанные руками (см. docstring) ---------------------

PNL = {
    "oct": {
        "revenue": "5100", "cogs": "1700", "losses": "0", "gross_profit": "3400",
        "opex": "25000", "cash_count": "-13.50", "ebitda": "-21613.50",
        "depreciation": "0", "disposal": "0", "operating_profit": "-21613.50",
        "interest": "0", "tax": "204.00", "net_profit": "-21817.50",
    },
    "nov": {
        "revenue": "2100", "cogs": "700", "losses": "200", "gross_profit": "1200",
        "opex": "25000", "cash_count": "0", "ebitda": "-23800",
        "depreciation": "2000.00", "disposal": "0", "operating_profit": "-25800.00",
        "interest": "1234.56", "tax": "84.00", "net_profit": "-27118.56",
    },
    "dec": {
        "revenue": "45000", "cogs": "15000", "losses": "0", "gross_profit": "30000",
        "opex": "33500", "cash_count": "7.25", "ebitda": "-3492.75",
        "depreciation": "2400.00", "disposal": "23600.00", "operating_profit": "-29492.75",
        "interest": "0", "tax": "1800.00", "net_profit": "-31292.75",
    },
}
QUARTER_PNL = {
    "revenue": "52200", "gross_profit": "34600", "ebitda": "-48906.25",
    "operating_profit": "-76906.25", "tax": "2088.00", "net_profit": "-80228.81",
}

CASH = {
    "oct": {
        "opening": "0", "operating": "-24827.97", "investing": "-120000", "financing": "150000",
        "net_flow": "5172.03", "outside": "50000", "unmatched": "0", "closing": "55172.03",
        "CASH": "14572.03", "BANK": "40600.00",
        "lines": {"clients": "4100", "suppliers": "-457.69", "cash_count": "-13.50",
                  "loan_in": "150000", "opening": "50000", "transfer": "0"},
    },
    "nov": {
        "opening": "55172.03", "operating": "2265.44", "investing": "-24000", "financing": "-5000",
        "net_flow": "-26734.56", "outside": "-3000", "unmatched": "-3000", "closing": "25437.47",
        "CASH": "8872.03", "BANK": "16565.44",
        "lines": {"clients": "4700", "refunds": "-1200", "interest_paid": "-1234.56",
                  "owner_out": "-5000", "transfer": "-3000"},
    },
    "dec": {
        "opening": "25437.47", "operating": "-13630.75", "investing": "0", "financing": "-10000",
        "net_flow": "-23630.75", "outside": "3000", "unmatched": "3000", "closing": "4806.72",
        "CASH": "241.28", "BANK": "4565.44",
        "lines": {"clients": "45000", "tax_paid": "-288", "cash_count": "7.25", "other": "150",
                  "loan_out": "-10000", "transfer": "3000"},
    },
}

BRIDGE = {
    "oct": {
        "net_profit": "-21817.50", "non_cash": "0", "receivables": "-1000", "client_money": "0",
        "inventory": "1242.31", "payables": "0", "accrued": "-3456.78", "tax_payable": "204.00",
        "capex": "-120000", "financing": "150000", "other": "0", "unexplained": "0",
    },
    "nov": {
        "net_profit": "-27118.56", "non_cash": "2000.00", "receivables": "1000", "client_money": "400",
        "inventory": "589", "payables": "311", "accrued": "25000", "tax_payable": "84.00",
        "capex": "-24000", "financing": "-5000", "other": "0", "unexplained": "0",
    },
    "dec": {
        "net_profit": "-31292.75", "non_cash": "26000.00", "receivables": "0", "client_money": "0",
        "inventory": "15000", "payables": "0", "accrued": "-25000", "tax_payable": "1512.00",
        "capex": "0", "financing": "-10000", "other": "150", "unexplained": "0",
    },
}


class GoldenQuarter(APITestCase):
    """Квартал из docstring модуля — собирается заново для каждого теста."""

    def setUp(self):
        self.admin = User.objects.create_user(username="golden", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.plate = Material.objects.create(
            name="Табличка", unit=Material.Unit.PIECE, quantity=D("1000"),
            purchase_price=D("100"), price_per_unit=D("300"),
        )
        self.screw = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=D("0"), purchase_price=D("12.37"),
        )
        self.build_quarter()

    # --- помощники ------------------------------------------------------------

    def sale(self, day, *qtys, method="CASH", paid=None, pay_full=False):
        return sale_service.create_sale(
            client=None, cashier=self.admin, payment_method=method,
            items_data=[{"type": "MATERIAL", "material": self.plate, "quantity": D(q), "mode": "PIECE"}
                        for q in qtys],
            amount_paid=D(paid) if paid is not None else None, pay_full=pay_full,
            created_at=noon(day),
        )

    def expense(self, code, amount, spent_at, account="CASH", period=None):
        payload = {"kind": ExpenseKind.objects.get(code=code).id, "amount": amount,
                   "spent_at": spent_at.isoformat(), "account": account}
        if period:
            payload["period"] = period
        r = self.client.post("/api/finance/expense-entries/", payload, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return ExpenseEntry.objects.get(pk=r.data["id"])

    def cash(self, article, kind, amount, day, account="CASH"):
        CashEntry.objects.create(account=account, kind=kind, article=article,
                                 amount=D(amount), happened_on=day)

    def checkout_online(self, qty):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "ONLINE",
            "items": [{"type": "MATERIAL", "material": self.plate.id, "quantity": qty, "mode": "PIECE"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    # --- квартал --------------------------------------------------------------

    def build_quarter(self):
        # Сентябрь: коммуналка за сентябрь, оплачена 15.10.
        self.expense("UTILITIES", "3456.78", date(2026, 10, 15), period="2026-09")

        # Октябрь
        self.cash("OPENING", "IN", "50000", date(2026, 10, 1))
        self.cash("LOAN_IN", "IN", "150000", date(2026, 10, 1), account="BANK")
        self.s1 = self.sale(date(2026, 10, 3), 6, 4, pay_full=True)
        self.s2 = self.sale(date(2026, 10, 5), 5, paid="500")
        # Онлайн-заказы оформляются сегодняшним днём (07.10.2026 на момент
        # написания) — для октября важно только, что сегодня октябрь 2026.
        self.o1 = self.checkout_online(2)
        sale_service.confirm_payment(self.o1)
        self.o2 = self.checkout_online(1)
        moment = noon(date(2026, 10, 7))
        Receipt.objects.filter(pk=self.o1.pk).update(created_at=moment, revenue_recognized_at=moment)
        Receipt.objects.filter(pk=self.o2.pk).update(created_at=moment)
        CashEntry.objects.filter(receipt=self.o1).update(happened_on=date(2026, 10, 7))
        self.expense("RENT", "25000", date(2026, 10, 10))
        receive_lot(self.screw, form=Roll.Form.PIECE, sheet_count=D("37"), purchase_cost=D("457.69"),
                    user=self.admin, received_at=noon(date(2026, 10, 20)), paid_account="CASH")
        self.machine = self.expense("EQUIPMENT", "120000", date(2026, 10, 25), account="BANK")
        self.cash("TRANSFER", "OUT", "10000", date(2026, 10, 28))
        self.cash("TRANSFER", "IN", "10000", date(2026, 10, 28), account="BANK")
        self.cash("COUNT", "OUT", "13.50", date(2026, 10, 31))

        # Ноябрь
        sale_service.apply_payment(self.s2, D("1000"), user=self.admin,
                                   paid_on=date(2026, 11, 2), method="CASH")
        self.s3 = self.sale(date(2026, 11, 5), 7, paid="2500")
        self.sale(date(2026, 11, 9), 4, method="MBANK", pay_full=True)
        self.computer = self.expense("EQUIPMENT", "24000", date(2026, 11, 10), account="BANK")
        receive_lot(self.screw, form=Roll.Form.PIECE, sheet_count=D("20"), purchase_cost=D("311.00"),
                    user=self.admin, received_at=noon(date(2026, 11, 12)), on_credit=True)
        InventoryLog.objects.create(
            type=InventoryLog.Type.WRITE_OFF, material=self.plate, quantity_changed=D("-2"),
            cost=D("200"), happened_at=noon(date(2026, 11, 14)),
        )
        self.expense("INTEREST", "1234.56", date(2026, 11, 20), account="BANK")
        self.cash("OWNER_OUT", "OUT", "5000", date(2026, 11, 25))
        self.cash("TRANSFER", "OUT", "3000", date(2026, 11, 28))
        line = self.s1.items.get(quantity=D("4"))
        sale_service.refund_receipt(self.s1, item_ids=[line.id], user=self.admin)
        type(line).objects.filter(pk=line.pk).update(returned_at=noon(date(2026, 11, 30)))
        CashEntry.objects.filter(receipt=self.s1, article="REFUND").update(happened_on=date(2026, 11, 30))
        self.expense("RENT", "25000", date(2026, 12, 3), account="BANK", period="2026-11")

        # Декабрь
        self.cash("TRANSFER", "IN", "3000", date(2026, 12, 1), account="BANK")
        self.expense("RENT", "25000", date(2026, 12, 4), account="BANK")
        self.sale(date(2026, 12, 10), 150, method="DEMIRBANK", pay_full=True)
        self.expense("EQUIPMENT", "8500", date(2026, 12, 12))
        self.expense("TAX", "288", date(2026, 12, 15))
        r = self.client.patch(f"/api/finance/expense-entries/{self.computer.id}/",
                              {"depreciate_until": "2026-12"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.cash("COUNT", "IN", "7.25", date(2026, 12, 20))
        self.cash("OTHER", "IN", "150", date(2026, 12, 22))
        self.cash("LOAN_OUT", "OUT", "10000", date(2026, 12, 27), account="BANK")

    # --- проверки ---------------------------------------------------------------

    def assertMoney(self, actual, expected, msg=None):
        self.assertEqual(D(actual), D(expected), msg)

    def test_pnl_each_month_by_hand(self):
        for name, (first, last) in MONTHS.items():
            p = pnl(first, last)
            got = {
                "revenue": p["revenue"], "cogs": p["cogs_material"] + p["cogs_services"],
                "losses": p["losses"], "gross_profit": p["gross_profit"],
                "opex": p["opex"]["total"] + p["opex_cash_manual"], "cash_count": p["cash_count"],
                "ebitda": p["ebitda"], "depreciation": p["depreciation"], "disposal": p["disposal"],
                "operating_profit": p["operating_profit"], "interest": p["interest"],
                "tax": p["tax"], "net_profit": p["net_profit"],
            }
            for key, expected in PNL[name].items():
                self.assertMoney(got[key], expected, f"{name}: {key}")

    def test_pnl_quarter_by_hand(self):
        p = pnl(*QUARTER)
        for key, expected in QUARTER_PNL.items():
            self.assertMoney(p[key], expected, key)
        self.assertEqual(p["tax_label"], "Налог (4 % от выручки)")

    def test_september_gets_its_utilities(self):
        sep = pnl(date(2026, 9, 1), date(2026, 9, 30))
        self.assertMoney(sep["opex"]["total"], "3456.78")
        self.assertMoney(sep["tax"], "0")                     # налог с октября
        self.assertMoney(sep["net_profit"], "-3456.78")

    def test_year_table_q4_columns_and_totals(self):
        year = pnl_year(2026)
        rows = {r["key"]: r for r in year["rows"]}
        for i, name in ((9, "oct"), (10, "nov"), (11, "dec")):
            self.assertMoney(rows["revenue"]["values"][i], PNL[name]["revenue"])
            self.assertMoney(rows["net"]["values"][i], PNL[name]["net_profit"])
            self.assertMoney(rows["tax"]["values"][i], -D(PNL[name]["tax"]))
        # Год: квартал + сентябрьская коммуналка.
        self.assertMoney(rows["net"]["total"], D(QUARTER_PNL["net_profit"]) - D("3456.78"))
        for r in year["rows"]:
            if r["kind"] != "percent":
                self.assertMoney(r["total"], sum(r["values"], D("0")), r["key"])
        # Подытоги = сумме своих строк.
        for i in range(12):
            self.assertMoney(
                rows["cogs"]["values"][i],
                rows["cogs_material"]["values"][i] + rows["cogs_services"]["values"][i]
                + rows["losses"]["values"][i],
            )
            self.assertMoney(rows["revenue"]["values"][i],
                             rows["revenue_material"]["values"][i] + rows["revenue_cutting"]["values"][i]
                             + rows["revenue_other"]["values"][i])
            blocks = [r for r in year["rows"] if r["key"].startswith("block:")]
            self.assertMoney(rows["opex"]["values"][i], sum((b["values"][i] for b in blocks), D("0")))

    def test_cash_flow_each_month_by_hand(self):
        for name, (first, last) in MONTHS.items():
            cf = cash_flow(first, last)
            exp = CASH[name]
            self.assertMoney(cf["opening"], exp["opening"], f"{name}: opening")
            self.assertMoney(cf["sections"]["operating"]["total"], exp["operating"], f"{name}: operating")
            self.assertMoney(cf["sections"]["investing"]["total"], exp["investing"], f"{name}: investing")
            self.assertMoney(cf["sections"]["financing"]["total"], exp["financing"], f"{name}: financing")
            self.assertMoney(cf["net_flow"], exp["net_flow"], f"{name}: net")
            self.assertMoney(cf["outside"], exp["outside"], f"{name}: outside")
            self.assertMoney(cf["unmatched_transfers"], exp["unmatched"], f"{name}: unmatched")
            self.assertMoney(cf["closing"], exp["closing"], f"{name}: closing")
            self.assertMoney(cf["by_account"]["CASH"]["closing"], exp["CASH"], f"{name}: CASH")
            self.assertMoney(cf["by_account"]["BANK"]["closing"], exp["BANK"], f"{name}: BANK")
            self.assertTrue(cf["balanced"], name)
            lines = {line["key"]: line["amount"] for s in cf["sections"].values() for line in s["lines"]}
            for key, expected in exp["lines"].items():
                self.assertMoney(lines.get(key, 0), expected, f"{name}: {key}")
            # Сумма строк раздела = итог раздела.
            for s in cf["sections"].values():
                self.assertMoney(sum((line["amount"] for line in s["lines"]), D("0")), s["total"])

    def test_closing_matches_the_real_cash_book(self):
        self.assertMoney(CashEntry.balance(upto=date(2026, 12, 31)), "4806.72")
        self.assertMoney(CashEntry.balance("CASH", upto=date(2026, 12, 31)), "241.28")
        self.assertMoney(CashEntry.balance("BANK", upto=date(2026, 12, 31)), "4565.44")

    def test_transfers_change_neither_total_nor_flow(self):
        """Октябрьский перевод сведён: вне потока 0 (там только ввод остатка).
        Ноябрь/декабрь: половины в разных месяцах — по месяцу «не сведены», за
        квартал ноль; поток от переводов не зависит ни в одном месяце."""
        q = cash_flow(*QUARTER)
        self.assertMoney(q["unmatched_transfers"], "0")
        self.assertMoney(q["outside"], "50000")                # только ввод остатка
        self.assertMoney(q["net_flow"], "-45193.28")
        self.assertMoney(q["closing"], D("0") + D("-45193.28") + D("50000"))
        year = cash_flow_year(2026)
        warn = next(r for r in year["rows"] if r["key"] == "unmatched_transfers")
        self.assertMoney(warn["values"][10], "-3000")
        self.assertMoney(warn["values"][11], "3000")
        self.assertTrue(year["balanced"])

    def test_bridge_each_month_by_hand(self):
        for name, (first, last) in MONTHS.items():
            b = bridge(first, last)
            lines = {line["key"]: line["amount"] for line in b["lines"]}
            for key, expected in BRIDGE[name].items():
                self.assertMoney(lines[key], expected, f"{name}: {key}")
            self.assertMoney(b["net_cash_flow"], CASH[name]["net_flow"], name)
            self.assertMoney(sum(lines.values(), D("0")), b["net_cash_flow"], name)

    def test_bridge_quarter_has_nothing_unexplained(self):
        b = bridge(*QUARTER)
        lines = {line["key"]: line["amount"] for line in b["lines"]}
        self.assertMoney(lines["net_profit"], QUARTER_PNL["net_profit"])
        self.assertMoney(lines["accrued"], "-3456.78")         # коммуналка сентября
        self.assertMoney(lines["tax_payable"], "1800.00")      # 2 088 начислено − 288 уплачено
        self.assertMoney(lines["unexplained"], "0")
        self.assertMoney(b["net_cash_flow"], "-45193.28")

    def test_every_screen_shows_the_same_profit(self):
        for name, (first, last) in MONTHS.items():
            expected = D(PNL[name]["net_profit"])
            self.assertMoney(pnl(first, last)["net_profit"], expected, f"{name}: ОПиУ")
            self.assertMoney(finance_summary(first, last)["profit"], expected, f"{name}: Сводка")
            self.assertMoney(headline(first, last)["net_profit"]["value"], expected, f"{name}: Обзор")
            self.assertMoney(daily_report(2026, first.month)["totals"]["profit"], expected, f"{name}: график")

    def test_depreciation_schedules(self):
        machine = depreciation.schedule(ExpenseEntry.objects.get(pk=self.machine.pk))
        self.assertEqual(min(machine), date(2026, 11, 1))
        self.assertEqual(len(machine), 60)
        self.assertMoney(sum(r + d for r, d in machine.values()), "120000")
        computer = depreciation.schedule(ExpenseEntry.objects.get(pk=self.computer.pk))
        self.assertEqual(computer, {date(2026, 12, 1): (D("400.00"), D("23600.00"))})

    def test_unpaid_online_order_is_nowhere(self):
        self.o2.refresh_from_db()
        self.assertIsNone(self.o2.revenue_recognized_at)
        self.assertMoney(self.o2.debt, "0")
        self.assertMoney(finance_summary(*OCT)["client_debt"], "0")   # S2 погасил в ноябре
        self.assertMoney(finance_summary(date(2026, 10, 1), date(2026, 10, 31))["revenue"], "5100")

    def test_overview_december_against_november(self):
        h = headline(*DEC)
        self.assertMoney(h["revenue"]["value"], "45000")
        self.assertMoney(h["revenue"]["change"]["before"], "2100")
        self.assertMoney(h["revenue"]["change"]["delta"], "42900")
        self.assertMoney(h["revenue"]["change"]["delta_pct"], "2042.9")
        self.assertMoney(h["net_cash_flow"]["value"], "-23630.75")
        self.assertMoney(h["cash_end"]["value"], "4806.72")
        self.assertMoney(h["cash_end"]["by_account"]["CASH"], "241.28")
        self.assertMoney(h["received"]["total"], "45000")
        self.assertMoney(h["why"]["unexplained"], "0")
