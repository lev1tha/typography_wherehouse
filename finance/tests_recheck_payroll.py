"""Перепроверка владельца 10.10 (docs/OWNER_RECHECK_2026-10-10.md), ведомость.

- RF-N1 (S1): гарантийная переделка шла в выработку мастера для премии —
  виновник брака получал премию за его исправление. Сценарий: Азамат режет
  70 пог.м, порог премии 80, гарантийная переделка — ещё 15 пог.м. Выработка 70,
  премии нет (в Excel ведомость октября 67 293, а не 70 293).
- RF-N2 (S1): начисление по ведомости и ручная трата «Зарплаты» за тот же месяц
  задваивались молча (D-100 обещал «нельзя»).
"""
from datetime import date

from finance import payroll
from finance.models import ExpenseEntry, ExpenseKind, PayScheme, RecurringExpense
from finance.reports.pnl import pnl
from finance.tests_payroll import OCT, PayrollCase
from finance.reports.work import machine_table

from decimal import Decimal as D

NOV = date(2026, 11, 1)


class WarrantyIsNotOutputTests(PayrollCase):
    def setUp(self):
        super().setUp()
        scheme = PayScheme.objects.get(employee=self.azamat)
        scheme.bonus_threshold = D("80")
        scheme.save(update_fields=["bonus_threshold"])
        self.work(date(2026, 10, 3), self.u_azamat, self.cnc, 40, 100)      # 4 000, 40 пог.м
        self.work(date(2026, 10, 8), self.u_azamat, self.cnc, 30, 100)      # 3 000, 30 пог.м
        # Гарантийная переделка: тот же мастер, 15 пог.м, бесплатно для клиента.
        self.warranty = self.work(date(2026, 10, 22), self.u_azamat, self.cnc, 15, 0)
        self.warranty.is_warranty = True
        self.warranty.save(update_fields=["is_warranty"])

    def test_warranty_meters_do_not_earn_the_bonus(self):
        row = self.row(payroll.statement(OCT), self.azamat)
        self.assertEqual(row["bonus"]["value"], D("70"))
        self.assertFalse(row["bonus"]["earned"])
        self.assertEqual(row["bonus"]["amount"], D("0"))
        cut = next(line for line in row["lines"] if line["work"] == "CUTTING_CNC")
        self.assertEqual((cut["base"], cut["meters"]), (D("7000"), D("70")))
        self.assertEqual(row["gross"], D("25420.00"))                        # 25 000 + 6 % × 7 000

    def test_warranty_paid_by_the_client_is_not_output_either(self):
        """Переделку не всегда делают бесплатно — деньги с неё тоже не выработка."""
        self.warranty.items.filter(type="SERVICE").update(price_per_item=D("50"))
        row = self.row(payroll.statement(OCT), self.azamat)
        self.assertEqual(row["percent_total"], D("420.00"))
        self.assertEqual(row["bonus"]["value"], D("70"))

    def test_machine_load_still_counts_the_rework(self):
        """«Резка по станкам» — загрузка станка: переделку станок тоже резал."""
        cnc = machine_table(OCT, date(2026, 10, 31))["cutting"]["CNC"]
        self.assertEqual(cnc["meters"], D("85"))


class SalaryOnceTests(PayrollCase):
    """Месяц ведётся ЛИБО ведомостью, ЛИБО ручной тратой «Зарплаты»."""

    def manual(self, period="2026-10", spent_at="2026-10-31", **extra):
        kind = ExpenseKind.objects.get(code=ExpenseKind.SALARY)
        body = {"kind": kind.id, "amount": "25000", "spent_at": spent_at, "account": "CASH",
                "name": "Азамат", "period": period, **extra}
        return self.client.post("/api/finance/expense-entries/", body, format="json")

    def accrue(self, month="2026-10"):
        return self.client.post("/api/finance/payroll/accrue/", {"month": month}, format="json")

    def salary_in_pnl(self, first, last):
        kind = ExpenseKind.objects.get(code=ExpenseKind.SALARY).id
        p = pnl(first, last)
        return sum((r["amount"] for b in p["opex"]["blocks"] for r in b["rows"] if r["kind_id"] == kind), D("0"))

    def test_manual_salary_after_accrual_is_refused(self):
        self.october()
        self.assertEqual(self.accrue().status_code, 200)
        accrued = self.salary_in_pnl(OCT, date(2026, 10, 31))
        r = self.manual()
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("ведомост", str(r.data))
        self.assertEqual(self.salary_in_pnl(OCT, date(2026, 10, 31)), accrued)

    def test_manual_salary_for_another_month_is_fine(self):
        self.october()
        self.accrue()
        r = self.manual(period="2026-09", spent_at="2026-10-02")
        self.assertEqual(r.status_code, 201, r.data)

    def test_accrual_over_manual_salary_is_refused(self):
        self.october()
        self.assertEqual(self.manual().status_code, 201)
        r = self.accrue()
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("вручную", str(r.data))
        self.assertFalse(ExpenseEntry.objects.filter(payroll_accrual__isnull=False).exists())
        self.assertEqual(self.salary_in_pnl(OCT, date(2026, 10, 31)), D("25000"))

    def test_old_manual_salary_without_period_counts_by_payment_date(self):
        self.october()
        kind = ExpenseKind.objects.get(code=ExpenseKind.SALARY)
        ExpenseEntry.objects.create(kind=kind, amount=D("1000"), spent_at=date(2026, 10, 3), period=None)
        self.assertEqual(self.accrue().status_code, 400)

    def test_moving_a_manual_salary_into_an_accrued_month_is_refused(self):
        self.october()
        r = self.manual(period="2026-09", spent_at="2026-09-30")
        self.assertEqual(r.status_code, 201, r.data)
        self.accrue()
        r2 = self.client.patch(f"/api/finance/expense-entries/{r.data['id']}/", {"period": "2026-10"}, format="json")
        self.assertEqual(r2.status_code, 400, r2.data)
        # другие поля старой записи править можно
        r3 = self.client.patch(f"/api/finance/expense-entries/{r.data['id']}/", {"note": "сентябрь"}, format="json")
        self.assertEqual(r3.status_code, 200, r3.data)

    def test_recurring_salary_rule_is_refused(self):
        kind = ExpenseKind.objects.get(code=ExpenseKind.SALARY)
        r = self.client.post("/api/finance/recurring/", {
            "kind": kind.id, "name": "ЗП Азамат", "amount": "25000", "day": 5,
            "start_month": "2026-09", "account": "CASH",
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(RecurringExpense.objects.exists())

    def test_old_salary_rule_skips_accrued_months(self):
        """Правило «Зарплаты», заведённое до запрета, в начисленный месяц не пишет."""
        self.october()
        self.accrue()
        kind = ExpenseKind.objects.get(code=ExpenseKind.SALARY)
        RecurringExpense.objects.create(
            kind=kind, name="ЗП", amount=D("25000"), day=1, start_month=date(2026, 9, 1), account="CASH")
        r = self.client.post("/api/finance/recurring/run/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        months = {d["month"] for d in r.data["details"]}
        self.assertNotIn(OCT, months)
        self.assertIn(date(2026, 9, 1), months)
        self.assertEqual(r.data["skipped_payroll"], 1)
