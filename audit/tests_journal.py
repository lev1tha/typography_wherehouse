"""Журнал действий: фильтры (дата, пользователь, тип, поиск) и страницы."""
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.kinds import classify
from audit.models import AuditLog

URL = "/api/audit/logs/"


class JournalFilterTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="boss", password="x", role=User.Role.ADMIN)
        self.other = User.objects.create_user(username="nurbek", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        self.a = AuditLog.record(self.admin, "Трата #1 «Аренда» изменена: сумма 100 → 120", kind="expense")
        self.b = AuditLog.record(self.other, "Оформлен чек 15 на 3000 сом")          # старая запись без типа
        self.c = AuditLog.record(self.admin, "Касса: расход 100 сом (Прочее, наличные)", kind="cash")
        self.d = AuditLog.record(self.admin, "Что-то совсем другое")
        AuditLog.objects.filter(pk=self.c.pk).update(created_at=timezone.now() - timedelta(days=10))

    def ids(self, **params):
        return {r["id"] for r in self.client.get(URL, params).data["results"]}

    def test_by_kind_stored_and_recognised_from_text(self):
        self.assertEqual(self.ids(kind="expense"), {self.a.id})
        self.assertEqual(self.ids(kind="order"), {self.b.id})           # узнана по тексту
        self.assertEqual(self.ids(kind="cash"), {self.c.id})
        self.assertEqual(self.ids(kind="other"), {self.d.id})

    def test_rows_report_their_kind(self):
        kinds = {r["id"]: r["kind"] for r in self.client.get(URL).data["results"]}
        self.assertEqual(kinds, {self.a.id: "expense", self.b.id: "order", self.c.id: "cash", self.d.id: "other"})

    def test_by_user_date_and_search(self):
        self.assertEqual(self.ids(user=self.other.id), {self.b.id})
        self.assertEqual(self.ids(username="nurb"), {self.b.id})
        today = timezone.localdate().isoformat()
        self.assertEqual(self.ids(date_from=today), {self.a.id, self.b.id, self.d.id})
        old = (timezone.localdate() - timedelta(days=10)).isoformat()
        self.assertEqual(self.ids(date_from=old, date_to=old), {self.c.id})
        # (SQLite ищет без учёта регистра только латиницу; на Postgres — любые буквы.)
        self.assertEqual(self.ids(search="Аренда"), {self.a.id})
        self.assertEqual(self.ids(search="чек", user=self.other.id), {self.b.id})

    def test_pagination(self):
        for n in range(30):
            AuditLog.record(self.admin, f"Трата #{n} добавлена", kind="expense")
        page1 = self.client.get(URL, {"page_size": 10}).data
        self.assertEqual((len(page1["results"]), page1["count"]), (10, 34))
        page4 = self.client.get(URL, {"page_size": 10, "page": 4}).data
        self.assertEqual(len(page4["results"]), 4)

    def test_accountant_reads_storekeeper_does_not(self):
        acc = User.objects.create_user(username="acc", password="x", role=User.Role.ACCOUNTANT)
        self.client.force_authenticate(acc)
        self.assertEqual(self.client.get(URL).status_code, 200)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(URL).status_code, 403)


class ClassifyTests(APITestCase):
    def test_known_prefixes(self):
        self.assertEqual(classify("Вход в систему"), "login")
        self.assertEqual(classify("Ставка налога: 4 % с 10.2026"), "tax")
        self.assertEqual(classify("Выплата зарплаты: Азамат"), "payroll")
        self.assertEqual(classify("Создана учётная запись «x»"), "staff")
        self.assertEqual(classify("???"), "other")
