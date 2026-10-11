"""Перепроверка владельца 10.10, S3 зарплаты (docs/OWNER_RECHECK_2026-10-10.md).

- RF-N3: выплата больше «к выдаче», за будущий месяц или отключённому
  сотруднику проходила молча → 409 `needs_confirmation`, повтор с
  `confirm_warnings` проводит.
- RF-N4: удержание больше начисленного пропадало → остаток переносится на
  следующий месяц и виден в ведомости.
- RF-N5: удаление сотрудника с правилами или выработкой по учётке стирало
  правила (CASCADE) и уводило выработку в «Без сотрудника» → 400.
"""
from datetime import date

from accounts.models import Employee, User
from finance import payroll
from finance.models import CashEntry, PayrollPayment, PayScheme
from finance.tests_payroll import D, NOV, OCT, PayrollCase

PAY = "/api/finance/payroll/payments/"


class PaymentWarningTests(PayrollCase):
    def pay(self, **body):
        data = {"employee": self.azamat.id, "kind": "PAYOUT", "amount": "1000", "paid_on": "2026-10-06", **body}
        return self.client.post(PAY, data, format="json")

    def test_payment_within_to_pay_goes_through(self):
        self.october()
        r = self.pay(amount="5000", period="2026-10")
        self.assertEqual(r.status_code, 201, r.data)

    def test_more_than_to_pay_asks_first(self):
        self.october()                                   # к выдаче за октябрь 28 670
        r = self.pay(amount="30000", period="2026-10")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertTrue(r.data["needs_confirmation"])
        self.assertEqual([w["code"] for w in r.data["warnings"]], ["over_to_pay"])
        self.assertIn("28 670", r.data["detail"])
        self.assertFalse(PayrollPayment.objects.exists())
        self.assertFalse(CashEntry.objects.exists())
        r = self.pay(amount="30000", period="2026-10", confirm_warnings=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(PayrollPayment.objects.count(), 1)

    def test_future_month_asks_first(self):
        r = self.pay(amount="100", period="2027-01")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn("future_month", [w["code"] for w in r.data["warnings"]])
        self.assertIn("01.2027", r.data["detail"])

    def test_inactive_employee_asks_first(self):
        self.october()
        self.azamat.is_active = False
        self.azamat.save(update_fields=["is_active"])
        r = self.pay(kind="ADVANCE", amount="1000", period="2026-10")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual([w["code"] for w in r.data["warnings"]], ["inactive"])
        self.assertEqual(self.pay(kind="ADVANCE", amount="1000", period="2026-10",
                                  confirm_warnings="true").status_code, 201)

    def test_closed_period_is_still_400_not_a_question(self):
        from finance.models import PeriodLock

        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 31)
        lock.save()
        r = self.pay(amount="999999", period="2027-01", paid_on="2026-10-05")
        self.assertEqual(r.status_code, 400)


class DeductionCarryTests(PayrollCase):
    def deduct(self, month, amount):
        r = self.client.post("/api/finance/payroll/adjustments/", {
            "employee": self.bakyt.id, "month": month, "reason": "FINE", "amount": amount,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_excess_deduction_moves_to_next_month(self):
        # Бакыт: оклад 22 000 с октября, выработки нет; штраф 30 000 за октябрь.
        self.deduct("2026-10", "30000")
        octo = self.row(payroll.statement(OCT), self.bakyt)
        self.assertEqual((octo["deductions"], octo["accrued"], octo["carry_out"]),
                         (D("30000"), D("0"), D("8000")))
        nov = self.row(payroll.statement(NOV), self.bakyt)
        self.assertEqual(nov["carry_in"], D("8000"))
        self.assertEqual(nov["deductions"], D("8000"))
        self.assertEqual(nov["accrued"], D("14000"))     # 22 000 − 8 000
        self.assertEqual(nov["carry_out"], D("0"))
        dec = self.row(payroll.statement(date(2026, 12, 1)), self.bakyt)
        self.assertEqual((dec["carry_in"], dec["accrued"]), (D("0"), D("22000")))

    def test_carry_chains_and_reaches_the_pnl_through_accrual(self):
        self.deduct("2026-10", "50000")                  # 22 000 окт + 22 000 ноя + 6 000 дек
        payroll.accrue(OCT, user=self.admin)
        nov = self.row(payroll.statement(NOV), self.bakyt)
        self.assertEqual((nov["carry_in"], nov["accrued"], nov["carry_out"]), (D("28000"), D("0"), D("6000")))
        payroll.accrue(NOV, user=self.admin)
        dec = self.row(payroll.statement(date(2026, 12, 1)), self.bakyt)
        self.assertEqual((dec["carry_in"], dec["accrued"]), (D("6000"), D("16000")))

    def test_inactive_employee_with_carry_stays_in_the_statement(self):
        self.deduct("2026-10", "30000")
        self.bakyt.is_active = False
        self.bakyt.save(update_fields=["is_active"])
        ids = [r["employee"]["id"] for r in payroll.statement(NOV)["rows"]]
        self.assertIn(self.bakyt.id, ids)

    def test_no_deductions_no_carry(self):
        self.october()
        self.assertEqual(payroll.carried_deductions(NOV), {})
        self.assertEqual(payroll.statement(NOV)["totals"]["carry_in"], D("0"))


class EmployeeDeleteTests(PayrollCase):
    def test_employee_with_rules_is_not_deleted(self):
        r = self.client.delete(f"/api/staff/employees/{self.bakyt.id}/")
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertIn("отключите", r.data["detail"])
        self.assertTrue(PayScheme.objects.filter(employee=self.bakyt).exists())

    def test_employee_with_output_by_login_is_not_deleted(self):
        PayScheme.objects.filter(employee=self.azamat).delete()
        self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 10, 100)
        r = self.client.delete(f"/api/staff/employees/{self.azamat.id}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Employee.objects.filter(pk=self.azamat.pk).exists())

    def test_clean_employee_is_deleted(self):
        u = User.objects.create_user(username="new_guy", password="x", role=User.Role.STOREKEEPER)
        e = Employee.objects.create(full_name="Новый", user=u)
        r = self.client.delete(f"/api/staff/employees/{e.id}/")
        self.assertEqual(r.status_code, 204)

    def test_rules_are_protected_at_the_model_level_too(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.bakyt.delete()
