"""Журнал действий, S3 перепроверки владельца 10.10.

- XL-07: правки видов расхода (создать, переименовать, скрыть, удалить, вернуть)
  следа не оставляли; правка сотрудника писала только «Сотрудник изменён: ФИО»
  без того, что изменилось.
- RU-N22: входы в систему заполняли журнал — по умолчанию скрыты (фильтр
  «показать входы» и «Тип: Вход» их возвращают); записи о поставщиках и
  накладных шли в «Прочее» — теперь «Склад»; суммы в тексте новых записей —
  с разрядами и запятой («12 000,50 сом»).
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import Employee, User
from audit.kinds import classify
from audit.models import AuditLog
from finance.auditing import fmt
from finance.models import ExpenseKind

URL = "/api/audit/logs/"


class ExpenseKindJournalTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="boss", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def last(self):
        return AuditLog.objects.order_by("-id").first()

    def test_create_rename_delete_leave_a_trace(self):
        r = self.client.post("/api/finance/expense-kinds/", {"name": "Охрана", "block": "FIXED", "role": "OPEX"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.data)
        log = self.last()
        self.assertEqual(log.kind, "expense")
        self.assertIn("Вид расхода добавлен: «Охрана»", log.action)

        r = self.client.patch(f"/api/finance/expense-kinds/{r.data['id']}/", {"name": "Охрана цеха"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("Вид расхода «Охрана» изменён: название Охрана → Охрана цеха", self.last().action)

        n = AuditLog.objects.count()
        self.client.patch(f"/api/finance/expense-kinds/{r.data['id']}/", {"name": "Охрана цеха"}, format="json")
        self.assertEqual(AuditLog.objects.count(), n)                    # без изменений — без записи

        r = self.client.delete(f"/api/finance/expense-kinds/{r.data['id']}/")
        self.assertEqual(r.status_code, 204)
        self.assertIn("Вид расхода удалён: «Охрана цеха»", self.last().action)

    def test_builtin_rename_and_archive_restore(self):
        rent = ExpenseKind.objects.get(code="RENT")
        r = self.client.patch(f"/api/finance/expense-kinds/{rent.id}/", {"name": "Аренда помещения"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(f"«{rent.name}» изменён: название {rent.name} → Аренда помещения", self.last().action)
        kind = ExpenseKind.objects.create(name="Реклама", block="VARIABLE")
        kind.entries.create(amount=Decimal("100"), spent_at="2026-10-01")
        self.assertEqual(self.client.delete(f"/api/finance/expense-kinds/{kind.id}/").status_code, 200)
        self.assertIn("Вид расхода скрыт: «Реклама»", self.last().action)
        self.client.post(f"/api/finance/expense-kinds/{kind.id}/restore/")
        self.assertIn("Вид расхода возвращён в отчёт: «Реклама»", self.last().action)
        self.assertEqual(classify("Вид расхода скрыт: «Реклама»"), "expense")


class EmployeeJournalTests(APITestCase):
    def test_employee_edit_lists_what_changed(self):
        admin = User.objects.create_user(username="boss", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(admin)
        e = Employee.objects.create(full_name="Азамат", default_machine="CNC", position="мастер")
        r = self.client.patch(f"/api/staff/employees/{e.id}/", {"default_machine": "LASER", "position": "старший мастер"},
                              format="json")
        self.assertEqual(r.status_code, 200, r.data)
        log = AuditLog.objects.order_by("-id").first()
        self.assertEqual(log.kind, "staff")
        self.assertIn("Сотрудник изменён: Азамат", log.action)
        self.assertIn("должность мастер → старший мастер", log.action)
        self.assertIn("станок ЧПУ → Лазер", log.action)
        self.client.patch(f"/api/staff/employees/{e.id}/", {"is_active": False}, format="json")
        self.assertIn("работает да → нет", AuditLog.objects.order_by("-id").first().action)


class JournalNoiseTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="boss", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.login_new = AuditLog.record(self.admin, "Вход в систему", kind="login")
        self.login_old = AuditLog.record(self.admin, "Вход в систему")          # до типа
        self.cash = AuditLog.record(self.admin, "Касса: приход 100 сом", kind="cash")

    def ids(self, **params):
        return {r["id"] for r in self.client.get(URL, params).data["results"]}

    def test_logins_hidden_by_default(self):
        self.assertEqual(self.ids(), {self.cash.id})
        self.assertEqual(self.ids(logins="1"), {self.login_new.id, self.login_old.id, self.cash.id})
        self.assertEqual(self.ids(kind="login"), {self.login_new.id, self.login_old.id})

    def test_csv_follows_the_same_rule(self):
        body = self.client.get(URL + "export/").content.decode("utf-8-sig")
        self.assertNotIn("Вход в систему", body)
        body = self.client.get(URL + "export/", {"logins": "1"}).content.decode("utf-8-sig")
        self.assertIn("Вход в систему", body)

    def test_token_login_is_typed(self):
        User.objects.create_user(username="kassa", password="secret-pass-1", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(None)
        r = self.client.post("/api/token/", {"username": "kassa", "password": "secret-pass-1"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(AuditLog.objects.order_by("-id").first().kind, "login")

    def test_supplier_and_invoice_records_are_stock(self):
        for text in (
            "Платёж поставщику: 5000 сом", "Платёж поставщику удалён: x", "Начальный долг поставщику «Акрил»: 100 сом",
            "Зачёт аванса поставщику от 01.10.2026: 100 сом", "Возврат поставщику по накладной Н-1: x",
            "Оплата по накладной Н-1: 100 KGS", "Дата накладной Н-1 перенесена", "Оплата поставщику за партию «x»",
            "Дописана оплата в кассу по накладной Н-1 от 01.10.2026",
        ):
            self.assertEqual(classify(text), "stock", text)
        log = AuditLog.record(self.admin, "Платёж поставщику: 5000 сом")
        self.assertEqual(self.client.get(URL, {"kind": "stock"}).data["results"][0]["id"], log.id)

    def test_money_format(self):
        self.assertEqual(fmt(Decimal("12000.50")), "12 000,50")
        self.assertEqual(fmt(Decimal("30000.00")), "30 000")
        self.assertEqual(fmt(Decimal("-1500.5")), "-1 500,50")
