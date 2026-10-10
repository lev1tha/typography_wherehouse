"""Ведомость зарплаты (аудит STAFF-01/-05/-09/-10, cash-11, G2-N2).

Сценарий владельца, октябрь 2026. Азамат (учётка, оклад 25 000, 6 % с резки,
10 % с монтажа, премия 3 000 за выработку больше 60 пог.м), Бакыт (лазер без
учётки, оклад 22 000, 5 % с резки, удержание за брак 1 411).

Суммы посчитаны руками:
  Азамат: резка ЧПУ 5 000 + 2 000 = 7 000 (70 пог.м), монтаж 2 500
          25 000 + 6 % × 7 000 (420) + 10 % × 2 500 (250) + премия 3 000 = 28 670
  Бакыт:  резка на лазере 3 000 (30 пог.м, премии нет): 22 000 + 5 % × 3 000 (150)
          = 22 150; удержано 1 411 → к оплате 20 739
  фонд зарплат за октябрь в ОПиУ: 28 670 + 20 739 = 49 409
"""
from datetime import date
from decimal import Decimal

from accounts.models import Employee, User
from finance import payroll
from finance.models import (
    CashEntry,
    ExpenseEntry,
    ExpenseKind,
    FinanceSettings,
    PayrollAccrual,
    PayrollPayment,
    PayScheme,
    PeriodLock,
)
from finance.reports import bridge as bridge_mod
from finance.reports.cashflow import cash_flow
from finance.reports.pnl import pnl
from finance.tests_reports_calc import PnlCase
from sales.models import TransactionItem
from services.models import PrintingService

D = Decimal
OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)


class PayrollCase(PnlCase):
    def setUp(self):
        super().setUp()
        self.cnc = PrintingService.objects.create(
            name="Резка ЧПУ", kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC)
        self.laser = PrintingService.objects.create(
            name="Резка лазер", kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.LASER)
        self.install = PrintingService.objects.create(
            name="Монтаж", kind=PrintingService.Kind.INSTALLATION)
        self.waste = PrintingService.objects.create(name="Отходы", kind=PrintingService.Kind.WASTE)

        self.u_azamat = User.objects.create_user(username="azamat", password="x", role=User.Role.STOREKEEPER)
        self.u_laser = User.objects.create_user(username="laser", password="x", role=User.Role.STOREKEEPER)
        self.azamat = Employee.objects.create(
            full_name="Азамат", user=self.u_azamat, default_machine=Employee.Machine.CNC)
        self.bakyt = Employee.objects.create(full_name="Бакыт", default_machine=Employee.Machine.LASER)

        scheme = PayScheme.objects.create(
            employee=self.azamat, valid_from=OCT, salary=D("25000"),
            bonus_threshold=D("60"), bonus_amount=D("3000"))
        scheme.rates.create(work="CUTTING", percent=D("6"))
        scheme.rates.create(work="INSTALL", percent=D("10"))
        scheme = PayScheme.objects.create(employee=self.bakyt, valid_from=OCT, salary=D("22000"))
        scheme.rates.create(work="CUTTING", percent=D("5"))

    def work(self, day, cashier, service, qty, price):
        """Заказ дня `day` с одной строкой услуги (материал в чек не нужен)."""
        receipt = self.sale(day)
        receipt.cashier = cashier
        receipt.save(update_fields=["cashier"])
        TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.SERVICE, service=service,
            quantity=D(qty), price_per_item=D(price))
        return receipt

    def october(self):
        self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 50, 100)     # 5 000, 50 пог.м
        self.work(date(2026, 10, 9), self.u_azamat, self.cnc, 20, 100)     # 2 000, 20 пог.м
        self.work(date(2026, 10, 12), self.u_azamat, self.install, 1, 2500)
        self.work(date(2026, 10, 14), self.u_laser, self.laser, 30, 100)   # 3 000 — по станку
        self.work(date(2026, 10, 15), self.u_laser, self.waste, 1, 800)    # отходы — не работа

    def row(self, stmt, employee):
        return next(r for r in stmt["rows"] if r["employee"]["id"] == employee.id)


class StatementTests(PayrollCase):
    def test_owner_scenario_matches_hand_calculation(self):
        self.october()
        stmt = payroll.statement(OCT)
        az, bk = self.row(stmt, self.azamat), self.row(stmt, self.bakyt)
        self.assertEqual(az["salary"], D("25000"))
        self.assertEqual(az["percent_total"], D("670.00"))          # 420 + 250
        self.assertTrue(az["bonus"]["earned"])                      # 70 пог.м > 60
        self.assertEqual(az["bonus"]["amount"], D("3000"))
        self.assertEqual(az["gross"], D("28670.00"))
        # Бакыт без своей учётки: выработка лазера идёт ему, потому что за лазером он один.
        self.assertEqual(bk["percent_total"], D("150.00"))
        self.assertFalse(bk["bonus"]["earned"])
        self.assertEqual(bk["gross"], D("22150.00"))
        # Отходы — не работа, процентов с них нет, и «Без сотрудника» их не показывает.
        self.assertEqual(stmt["unassigned"], [])

    def test_threshold_is_strict_and_bonus_off_without_threshold(self):
        self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 60, 100)        # ровно порог
        self.assertFalse(self.row(payroll.statement(OCT), self.azamat)["bonus"]["earned"])
        self.work(date(2026, 10, 6), self.u_azamat, self.cnc, "0.5", 100)
        self.assertTrue(self.row(payroll.statement(OCT), self.azamat)["bonus"]["earned"])

    def test_machine_with_two_masters_is_not_guessed(self):
        """Два мастера за ЧПУ и общая учётка: деньги не делим наугад, строка «Без сотрудника»."""
        Employee.objects.create(full_name="Мирлан", default_machine=Employee.Machine.CNC)
        shared = User.objects.create_user(username="chpu", password="x", role=User.Role.STOREKEEPER)
        self.work(date(2026, 10, 5), shared, self.cnc, 10, 100)
        stmt = payroll.statement(OCT)
        self.assertEqual([(u["work"], u["base"]) for u in stmt["unassigned"]],
                         [("CUTTING_CNC", D("1000"))])
        self.assertEqual(self.row(stmt, self.azamat)["percent_total"], D("0"))

    def test_machine_rate_beats_the_general_one(self):
        scheme = PayScheme.objects.get(employee=self.azamat)
        scheme.rates.create(work="CUTTING_CNC", percent=D("8"))
        self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 10, 100)         # 1 000 × 8 % = 80
        self.assertEqual(self.row(payroll.statement(OCT), self.azamat)["percent_total"], D("80.00"))

    def test_return_in_month_takes_the_work_out(self):
        receipt = self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 10, 100)
        line = receipt.items.get(type="SERVICE")
        TransactionItem.objects.filter(pk=line.pk).update(
            is_returned=True, returned_at=self.noon(date(2026, 10, 7)))
        self.assertEqual(self.row(payroll.statement(OCT), self.azamat)["percent_total"], D("0"))

    def noon(self, day):
        from finance.tests_reports_calc import noon
        return noon(day)

    def test_rules_have_a_history(self):
        """С ноября ставка выше — октябрь считается по прежней."""
        self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 10, 100)
        self.work(date(2026, 11, 5), self.u_azamat, self.cnc, 10, 100)
        new = PayScheme.objects.create(employee=self.azamat, valid_from=NOV, salary=D("30000"))
        new.rates.create(work="CUTTING", percent=D("10"))
        self.assertEqual(self.row(payroll.statement(OCT), self.azamat)["gross"], D("25060.00"))
        self.assertEqual(self.row(payroll.statement(NOV), self.azamat)["gross"], D("30100.00"))


class AccrualTests(PayrollCase):
    def accrue(self):
        return payroll.accrue(OCT, user=self.admin)

    def test_accrual_hits_the_pnl_but_not_the_cash(self):
        self.october()
        PayrollAdjustmentFactory.fine(self, self.bakyt, "1411")
        self.accrue()
        self.assertEqual(
            sorted(PayrollAccrual.objects.values_list("employee__full_name", "amount")),
            [("Азамат", D("28670.00")), ("Бакыт", D("20739.00"))],
        )
        p = pnl(OCT, date(2026, 10, 31))
        salary_row = next(r for b in p["opex"]["blocks"] for r in b["rows"] if r["name"] == "Зарплаты")
        self.assertEqual(salary_row["amount"], D("49409.00"))
        self.assertFalse(CashEntry.objects.exists())                  # деньги не двигались
        entries = ExpenseEntry.objects.filter(is_cashless=True)
        self.assertEqual(entries.count(), 2)

    def test_repeat_is_a_recalculation_not_a_duplicate(self):
        self.october()
        self.accrue()
        self.work(date(2026, 10, 20), self.u_azamat, self.cnc, 10, 100)       # ещё 1 000 × 6 % = 60
        self.accrue()
        self.assertEqual(PayrollAccrual.objects.count(), 2)
        self.assertEqual(ExpenseEntry.objects.filter(is_cashless=True).count(), 2)
        self.assertEqual(PayrollAccrual.objects.get(employee=self.azamat).amount, D("28730.00"))

    def test_closed_month_cannot_be_accrued(self):
        self.october()
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 31)
        lock.save()
        with self.assertRaises(Exception) as ctx:
            self.accrue()
        self.assertIn("период закрыт", str(ctx.exception.detail[0]))

    def test_unpost_removes_the_expense(self):
        self.october()
        self.accrue()
        self.assertEqual(payroll.unpost(OCT), 2)
        self.assertFalse(ExpenseEntry.objects.filter(is_cashless=True).exists())

    def test_accrual_expense_cannot_be_edited_or_deleted_by_hand(self):
        self.october()
        self.accrue()
        entry = ExpenseEntry.objects.filter(is_cashless=True).first()
        r = self.client.delete(f"/api/finance/expense-entries/{entry.id}/")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"/api/finance/expense-entries/{entry.id}/", {"amount": "1"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(ExpenseEntry.objects.filter(pk=entry.pk).exists())

    def test_a_deduction_beyond_gross_is_not_a_negative_expense(self):
        PayrollAdjustmentFactory.fine(self, self.bakyt, "30000")             # оклад 22 000
        self.accrue()
        self.assertEqual(PayrollAccrual.objects.get(employee=self.bakyt).amount, D("0"))
        # Расход есть только у Азамата (оклад): у Бакыта вычет съел всё начисление.
        self.assertEqual(
            list(ExpenseEntry.objects.filter(is_cashless=True).values_list("name", flat=True)), ["Азамат"])


class PaymentTests(PayrollCase):
    def setUp(self):
        super().setUp()
        self.october()
        payroll.accrue(OCT, user=self.admin)

    def test_advance_and_payout_write_the_cash_book(self):
        a = payroll.pay(self.azamat, kind="ADVANCE", amount=D("10000"), paid_on=date(2026, 10, 5),
                        user=self.admin)
        self.assertEqual(a.period, OCT)                                  # аванс — за текущий месяц
        entry = a.cash_entry
        self.assertEqual((entry.kind, entry.article, entry.account), ("OUT", "PAYROLL", "CASH"))
        self.assertTrue(entry.is_auto)
        self.assertEqual(CashEntry.balance("CASH"), D("-10000"))
        stmt = payroll.statement(OCT)
        az = self.row(stmt, self.azamat)
        self.assertEqual((az["accrued"], az["advances"], az["to_pay"]), (D("28670.00"), D("10000"), D("18670.00")))
        # Расчёт 3 ноября по умолчанию — за октябрь (настройка: до 31-го числа — за прошлый месяц).
        p = payroll.pay(self.azamat, kind="PAYOUT", amount=D("18670"), paid_on=date(2026, 11, 3),
                        account="BANK", user=self.admin)
        self.assertEqual(p.period, OCT)
        az = self.row(payroll.statement(OCT), self.azamat)
        self.assertEqual((az["payouts"], az["to_pay"]), (D("18670"), D("0.00")))
        self.assertEqual(CashEntry.balance("BANK"), D("-18670"))

    def test_default_period_follows_the_setting(self):
        settings = FinanceSettings.load()
        settings.payroll_prev_month_until_day = 10
        settings.save()
        self.assertEqual(payroll.default_period("PAYOUT", date(2026, 11, 10)), OCT)
        self.assertEqual(payroll.default_period("PAYOUT", date(2026, 11, 11)), NOV)
        self.assertEqual(payroll.default_period("ADVANCE", date(2026, 11, 3)), NOV)
        settings.payroll_prev_month_until_day = 0
        settings.save()
        self.assertEqual(payroll.default_period("PAYOUT", date(2026, 11, 3)), NOV)

    def test_payments_do_not_change_the_pnl_and_the_bridge_stays_at_zero(self):
        before = pnl(OCT, date(2026, 10, 31))["net_profit"]
        payroll.pay(self.azamat, kind="ADVANCE", amount=D("10000"), paid_on=date(2026, 10, 5), user=self.admin)
        payroll.pay(self.azamat, kind="PAYOUT", amount=D("18670"), paid_on=date(2026, 11, 3), user=self.admin)
        self.assertEqual(pnl(OCT, date(2026, 10, 31))["net_profit"], before)
        for first, last in ((OCT, date(2026, 10, 31)), (NOV, date(2026, 11, 30)), (OCT, date(2026, 11, 30))):
            with self.subTest(period=(first, last)):
                self.assertEqual(bridge_mod.bridge(first, last)["unexplained"], D("0"))
        cf = cash_flow(OCT, date(2026, 11, 30))
        lines = {l["key"]: l["amount"] for l in cf["sections"]["operating"]["lines"]}
        self.assertEqual(lines["payroll"], D("-28670"))

    def test_remove_payment_removes_the_cash_row(self):
        p = payroll.pay(self.azamat, kind="ADVANCE", amount=D("10000"), paid_on=date(2026, 10, 5), user=self.admin)
        payroll.remove_payment(p)
        self.assertFalse(CashEntry.objects.exists())
        self.assertFalse(PayrollPayment.objects.exists())


class PayrollAdjustmentFactory:
    @staticmethod
    def fine(case, employee, amount, month=OCT):
        from finance.models import PayrollAdjustment
        return PayrollAdjustment.objects.create(
            employee=employee, month=month, reason="DEFECT", amount=D(amount), note="брак")


class PayrollApiTests(PayrollCase):
    def test_percent_is_validated_between_0_and_100(self):
        body = {
            "employee": self.azamat.id, "valid_from": "2026-12", "salary": "1000",
            "rates": [{"work": "CUTTING", "percent": "101"}],
        }
        r = self.client.post("/api/finance/pay-schemes/", body, format="json")
        self.assertEqual(r.status_code, 400)
        body["rates"] = [{"work": "CUTTING", "percent": "-1"}]
        self.assertEqual(self.client.post("/api/finance/pay-schemes/", body, format="json").status_code, 400)
        body["rates"] = [{"work": "CUTTING", "percent": "100"}, {"work": "INSTALL", "percent": "0"}]
        r = self.client.post("/api/finance/pay-schemes/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_statement_accrue_pay_and_csv_over_api(self):
        self.october()
        r = self.client.get("/api/finance/payroll/?month=2026-10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["rows"]), 2)
        r = self.client.post("/api/finance/payroll/accrue/", {"month": "2026-10"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        r = self.client.post("/api/finance/payroll/payments/", {
            "employee": self.azamat.id, "kind": "ADVANCE", "amount": "5000", "paid_on": "2026-10-06",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["period"], "2026-10")
        csv = self.client.get("/api/finance/payroll/export/?month=2026-10")
        self.assertEqual(csv.status_code, 200)
        body = csv.content.decode("utf-8-sig")
        self.assertIn("Азамат", body)
        self.assertIn("28670,00", body)
        self.assertIn("ИТОГО", body)

    def test_accountant_reads_but_cannot_write(self):
        acc = User.objects.create_user(username="acc", password="x", role=User.Role.ACCOUNTANT)
        self.client.force_authenticate(acc)
        self.assertEqual(self.client.get("/api/finance/payroll/?month=2026-10").status_code, 200)
        r = self.client.post("/api/finance/payroll/accrue/", {"month": "2026-10"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_payment_is_audited(self):
        from audit.models import AuditLog
        self.client.post("/api/finance/payroll/payments/", {
            "employee": self.azamat.id, "kind": "ADVANCE", "amount": "5000", "paid_on": "2026-10-06",
        }, format="json")
        row = AuditLog.objects.filter(kind="payroll").first()
        self.assertIn("Азамат", row.action)
        self.assertIn("5 000", row.action)


class BuiltinKindsTests(PnlCase):
    def test_new_builtin_kinds_exist_in_the_variable_block(self):
        for code, name in (("BAD_DEBT", "Безнадёжные долги"), ("WARRANTY", "Гарантийные переделки"),
                           ("FX_DIFF", "Курсовая разница")):
            kind = ExpenseKind.objects.get(code=code)
            self.assertEqual((kind.name, kind.block, kind.role, kind.is_builtin),
                             (name, "VARIABLE", "OPEX", True))

    def test_bad_debt_has_no_cash_row_but_fx_diff_has(self):
        self.expense("BAD_DEBT", "12000", date(2026, 10, 10))
        self.assertFalse(CashEntry.objects.exists())                       # долг списан, денег не было
        self.expense("FX_DIFF", "700", date(2026, 10, 11))
        self.assertEqual(CashEntry.objects.get().amount, D("700"))

    def test_warranty_kind_is_archived(self):
        """Волна 2: переделки — строкой себестоимости ОПиУ (`cogs_warranty`), вид
        расходов скрыт, чтобы их не внесли тратой второй раз."""
        self.assertTrue(ExpenseKind.objects.get(code="WARRANTY").is_archived)

    def test_bad_debt_is_a_pnl_expense_and_the_bridge_stays_at_zero(self):
        self.sale(date(2026, 10, 5), qty=40, paid="0")                     # 12 000 в долг
        self.expense("BAD_DEBT", "12000", date(2026, 10, 20))
        p = pnl(OCT, date(2026, 10, 31))
        self.assertEqual(p["opex_noncash"], D("12000.00"))
        self.assertEqual(p["net_profit"], D("12000") - D("4000") - D("12000") - D("480"))   # налог 4 % = 480
        b = bridge_mod.bridge(OCT, date(2026, 10, 31))
        self.assertEqual(b["unexplained"], D("0"))
        keys = {l["key"]: l["amount"] for l in b["lines"]}
        self.assertEqual(keys["written_off"], D("12000.00"))
        self.assertEqual(keys["accrued"], D("0.00"))


class AdjustmentApiTests(PayrollCase):
    URL = "/api/finance/payroll/adjustments/"

    def test_defect_deduction_links_to_the_stock_writeoff(self):
        from warehouse.models import InventoryLog

        log = InventoryLog.objects.create(
            material=self.plate, type=InventoryLog.Type.WRITE_OFF, quantity_changed=D("-3"),
            reason="брак при резке", cost=D("300"))
        r = self.client.post(self.URL, {
            "employee": self.bakyt.id, "month": "2026-10", "reason": "DEFECT", "amount": "1411",
            "note": "лист испорчен", "inventory_log": log.id}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["inventory_log"], log.id)
        stmt = payroll.statement(OCT)
        row = self.row(stmt, self.bakyt)
        self.assertEqual(row["deductions"], D("1411"))
        self.assertEqual(row["adjustments"][0]["inventory_log"], log.id)

    def test_amount_must_be_positive_and_closed_month_is_refused(self):
        body = {"employee": self.bakyt.id, "month": "2026-10", "reason": "FINE", "amount": "0"}
        self.assertEqual(self.client.post(self.URL, body, format="json").status_code, 400)
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 31)
        lock.save()
        body["amount"] = "100"
        r = self.client.post(self.URL, body, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("период закрыт", str(r.data))


class PaymentLockAndFeedTests(PayrollCase):
    def test_payment_in_a_closed_period_is_refused(self):
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 10, 31)
        lock.save()
        r = self.client.post("/api/finance/payroll/payments/", {
            "employee": self.azamat.id, "kind": "ADVANCE", "amount": "100", "paid_on": "2026-10-05",
        }, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(CashEntry.objects.exists())

    def test_accrual_rows_stay_out_of_the_paid_expenses_feed(self):
        self.october()
        payroll.accrue(OCT, user=self.admin)
        feed = self.client.get("/api/finance/expense-entries/feed/", {"date_from": "2026-10-01", "date_to": "2026-10-31"})
        self.assertEqual(feed.data["results"], [])
        self.assertEqual(feed.data["total"], D("0"))

    def test_report_row_of_salary_shows_the_accrual(self):
        self.october()
        payroll.accrue(OCT, user=self.admin)
        r = self.client.get("/api/finance/report/", {"date_from": "2026-10-01", "date_to": "2026-10-31"})
        salary = next(x for x in r.data["fixed"]["rows"] if x["code"] == "SALARY")
        # Без удержаний: 28 670 (Азамат) + 22 150 (Бакыт).
        self.assertEqual(D(str(salary["amount"])), D("50820.00"))
