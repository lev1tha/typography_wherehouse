"""PNL-09 (перепроверка 10.10, S3): «что если» не учитывал проценты мастеров.

+10 % к цене услуг поднимает и зарплату мастеров, которые получают процент от
стоимости работы, — прогноз прибыли без этого завышен (в сценарии владельца на
179 сом за октябрь). Сценарий `PayrollCase.october()`: Азамат 6 % с резки ЧПУ
7 000 и 10 % с монтажа 2 500, Бакыт 5 % с лазера 3 000 → 420 + 250 + 150 = 820.
"""
from datetime import date

from accounts.models import Employee
from finance.reports.whatif import what_if_year
from finance.tests_payroll import D, PayrollCase
from sales.models import TransactionItem


class WhatIfMastersTests(PayrollCase):
    def rows(self, price=10, cost=0):
        return {r["key"]: r for r in what_if_year(2026, price, cost)["rows"]}

    def test_master_percent_grows_with_service_price(self):
        self.october()
        rows = self.rows()
        self.assertEqual(rows["masters"]["base"][9], D("820.00"))
        self.assertEqual(rows["masters"]["delta"][9], D("82.00"))
        d_rev = rows["revenue"]["delta"][9]
        self.assertEqual(rows["gross"]["delta"][9], d_rev)                 # процент — не себестоимость
        self.assertEqual(rows["ebitda"]["delta"][9], d_rev - D("82.00"))
        self.assertEqual(rows["net"]["delta"][9], d_rev - D("82.00") - rows["tax"]["delta"][9])

    def test_line_without_rules_uses_the_average_rate(self):
        self.october()
        mirlan = Employee.objects.create(full_name="Мирлан")             # правил оплаты нет
        receipt = self.sale(date(2026, 10, 20))
        TransactionItem.objects.create(receipt=receipt, type="SERVICE", service=self.install,
                                       quantity=D("1"), price_per_item=D("1000"), executor=mirlan)
        rows = self.rows()
        # Средняя ставка месяца 820 / 12 500 = 6,56 % → +65,60 с 1 000.
        self.assertEqual(rows["masters"]["base"][9], D("885.60"))
        self.assertEqual(rows["masters"]["delta"][9], D("88.56"))

    def test_warranty_rework_pays_no_percent(self):
        self.october()
        rework = self.work(date(2026, 10, 22), self.u_azamat, self.cnc, 10, 100)
        rework.is_warranty = True
        rework.save(update_fields=["is_warranty"])
        self.assertEqual(self.rows()["masters"]["base"][9], D("820.00"))

    def test_zero_scenario_is_still_the_base(self):
        self.october()
        for row in what_if_year(2026, 0, 0)["rows"]:
            self.assertEqual(row["base"], row["scenario"], row["key"])

    def test_api_returns_the_masters_row(self):
        self.october()
        r = self.client.get("/api/finance/pnl/what-if/", {"year": 2026, "price_pct": "10"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("masters", [row["key"] for row in r.data["rows"]])
        self.assertIn("мастер", r.data["assumptions"])
