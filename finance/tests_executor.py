"""Исполнитель в строке работы (волна 2, STAFF-02/F10).

Мастера работают под общими логинами, и «кто оформил» — не «кто резал». Поле
`TransactionItem.executor` читается ведомостью и «Резкой по сотрудникам»
первым; строки без исполнителя считаются как раньше.
"""
from datetime import date
from decimal import Decimal

from audit.models import AuditLog
from finance import payroll
from finance.reports.summary import finance_summary
from finance.tests_payroll import OCT, PayrollCase
from sales.models import TransactionItem

D = Decimal


class ExecutorCase(PayrollCase):
    def setUp(self):
        super().setUp()
        self.install.base_price = D("2500")
        self.install.save(update_fields=["base_price"])

    def checkout(self, items, user=None):
        if user is not None:
            self.client.force_authenticate(user)
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "pay_full": True, "items": items},
            format="json",
        )


class CheckoutExecutorTests(ExecutorCase):
    def test_executor_saved_on_work_line(self):
        r = self.checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1,
                            "executor": self.bakyt.id}])
        self.assertEqual(r.status_code, 201, r.data)
        line = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertEqual(line.executor_id, self.bakyt.id)
        self.assertEqual(r.data["items"][0]["executor_name"], "Бакыт")

    def test_executor_is_optional(self):
        r = self.checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(TransactionItem.objects.get(receipt_id=r.data["id"]).executor_id)

    def test_material_line_has_no_executor(self):
        r = self.checkout([{"type": "MATERIAL", "material": self.plate.id, "quantity": 1,
                            "mode": "PIECE", "executor": self.bakyt.id}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_inactive_employee_rejected(self):
        self.bakyt.is_active = False
        self.bakyt.save(update_fields=["is_active"])
        r = self.checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1,
                            "executor": self.bakyt.id}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_storekeeper_sees_executors_list(self):
        self.client.force_authenticate(self.u_azamat)
        r = self.client.get("/api/staff/employees/executors/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["me"], self.azamat.id)
        self.assertEqual({e["full_name"] for e in r.data["employees"]}, {"Азамат", "Бакыт"})
        # Полный справочник (учётки, примечания) складовщику по-прежнему закрыт.
        self.assertEqual(self.client.get("/api/staff/employees/").status_code, 403)


class PayrollByExecutorTests(ExecutorCase):
    def test_explicit_executor_wins_old_lines_as_before(self):
        # Оформил Азамат, а резал Бакыт — выработка Бакыта.
        receipt = self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 40, 100)
        receipt.items.filter(type="SERVICE").update(executor=self.bakyt)
        # Старая строка без исполнителя — как раньше: учётка кассира → Азамат.
        self.work(date(2026, 10, 6), self.u_azamat, self.cnc, 10, 100)
        out = payroll.output(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(out[self.bakyt.id]["CUTTING_CNC"]["amount"], D("4000"))
        self.assertEqual(out[self.azamat.id]["CUTTING_CNC"]["amount"], D("1000"))
        stmt = payroll.statement(OCT)
        bk = self.row(stmt, self.bakyt)
        self.assertEqual(bk["percent_total"], D("200.00"))          # 5 % × 4 000

    def test_cutting_by_user_uses_executor(self):
        receipt = self.work(date(2026, 10, 5), self.u_azamat, self.cnc, 40, 100)
        receipt.items.filter(type="SERVICE").update(executor=self.bakyt)
        self.work(date(2026, 10, 6), self.u_azamat, self.cnc, 10, 100)
        rows = {u["name"]: u for u in finance_summary(date(2026, 10, 1), date(2026, 10, 31))["cutting"]["by_user"]}
        self.assertEqual(rows["Бакыт"]["amount"], D("4000"))
        self.assertEqual(rows["Бакыт"]["kind"], "employee")
        self.assertEqual(rows["azamat"]["amount"], D("1000"))       # без исполнителя — логин
        self.assertEqual(rows["azamat"]["kind"], "user")


class EditExecutorTests(ExecutorCase):
    def test_admin_changes_executor_with_journal_and_no_money(self):
        r = self.checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1}])
        receipt_id = r.data["id"]
        line = TransactionItem.objects.get(receipt_id=receipt_id)
        before = (r.data["total_price"], r.data["amount_paid"], r.data["change_due"])
        r = self.client.post(f"/api/sales/receipts/{receipt_id}/edit-items/",
                             {"items": [{"id": line.id, "executor": self.bakyt.id}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        line.refresh_from_db()
        self.assertEqual(line.executor_id, self.bakyt.id)
        self.assertEqual((r.data["total_price"], r.data["amount_paid"], r.data["change_due"]), before)
        log = AuditLog.objects.filter(action__startswith="Исполнитель в чеке").last()
        self.assertIn("не указан → Бакыт", log.action)
        self.assertFalse(AuditLog.objects.filter(action__startswith="Правка состава чека").exists())
        # Снять исполнителя тоже можно.
        r = self.client.post(f"/api/sales/receipts/{receipt_id}/edit-items/",
                             {"items": [{"id": line.id, "executor": None}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        line.refresh_from_db()
        self.assertIsNone(line.executor_id)

    def test_storekeeper_cannot_edit(self):
        r = self.checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1}])
        line = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.client.force_authenticate(self.u_azamat)
        r = self.client.post(f"/api/sales/receipts/{r.data['id']}/edit-items/",
                             {"items": [{"id": line.id, "executor": self.bakyt.id}]}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_employee_with_work_cannot_be_deleted(self):
        self.checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1,
                        "executor": self.bakyt.id}])
        r = self.client.delete(f"/api/staff/employees/{self.bakyt.id}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(TransactionItem.objects.filter(executor=self.bakyt).exists())
