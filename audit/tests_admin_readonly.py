"""Журнал действий в Django-админке — только чтение, даже для суперпользователя."""
from django.test import TestCase

from accounts.models import User
from audit.models import AuditLog


class AuditLogAdminReadOnlyTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser(username="root", password="pw-root-1")
        self.client.force_login(self.root)
        self.entry = AuditLog.record(self.root, "Вход в систему")

    def _get(self, path):
        return self.client.get(path, HTTP_HOST="localhost")

    def test_can_view_list_and_entry(self):
        self.assertEqual(self._get("/django-admin/audit/auditlog/").status_code, 200)
        self.assertEqual(
            self._get(f"/django-admin/audit/auditlog/{self.entry.pk}/change/").status_code, 200
        )

    def test_cannot_add(self):
        self.assertEqual(self._get("/django-admin/audit/auditlog/add/").status_code, 403)

    def test_cannot_change(self):
        r = self.client.post(
            f"/django-admin/audit/auditlog/{self.entry.pk}/change/",
            {"action": "подделка"},
            HTTP_HOST="localhost",
        )
        self.assertEqual(r.status_code, 403)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.action, "Вход в систему")

    def test_cannot_delete(self):
        r = self.client.post(
            f"/django-admin/audit/auditlog/{self.entry.pk}/delete/",
            {"post": "yes"},
            HTTP_HOST="localhost",
        )
        self.assertEqual(r.status_code, 403)
        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())
        # И массовое удаление из списка тоже.
        r = self.client.post(
            "/django-admin/audit/auditlog/",
            {"action": "delete_selected", "_selected_action": [self.entry.pk], "post": "yes"},
            HTTP_HOST="localhost",
        )
        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())
