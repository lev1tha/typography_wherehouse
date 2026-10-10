"""Экран «Сотрудники» (STAFF-11): учётки и справочник сотрудников."""
from rest_framework.test import APITestCase

from accounts.models import Employee, User
from audit.models import AuditLog


class StaffUsersTests(APITestCase):
    URL = "/api/staff/users/"

    def setUp(self):
        self.admin = User.objects.create_user(username="boss", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def test_create_user_with_role_and_login_works(self):
        r = self.client.post(self.URL, {
            "username": "nurbek", "password": "Qwerty-2026", "role": "STOREKEEPER",
            "first_name": "Нурбек",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertNotIn("password", r.data)
        user = User.objects.get(username="nurbek")
        self.assertTrue(user.check_password("Qwerty-2026"))
        self.assertEqual(user.role, "STOREKEEPER")
        login = self.client_class().post("/api/token/", {"username": "nurbek", "password": "Qwerty-2026"}, format="json")
        self.assertEqual(login.status_code, 200)

    def test_weak_or_missing_password_is_refused(self):
        for password in ("12345678", "short", ""):
            r = self.client.post(self.URL, {"username": "u" + password, "password": password, "role": "STOREKEEPER"},
                                 format="json")
            self.assertEqual(r.status_code, 400, password)
            self.assertIn("password", r.data, password)       # ошибка у поля, а не общая
        r = self.client.post(self.URL, {"username": "nopass", "role": "STOREKEEPER"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_duplicate_login_ignores_case(self):
        User.objects.create_user(username="Aibek", password="x")
        r = self.client.post(self.URL, {"username": "aibek", "password": "Qwerty-2026", "role": "STOREKEEPER"},
                             format="json")
        self.assertEqual(r.status_code, 400)

    def test_change_role_and_audit(self):
        u = User.objects.create_user(username="u1", password="x", role=User.Role.STOREKEEPER)
        r = self.client.patch(f"{self.URL}{u.id}/", {"role": "ACCOUNTANT"}, format="json")
        self.assertEqual(r.status_code, 200)
        u.refresh_from_db()
        self.assertEqual(u.role, "ACCOUNTANT")
        self.assertTrue(AuditLog.objects.filter(kind="staff", action__contains="Бухгалтер").exists())

    def test_set_password_revokes_old_tokens(self):
        u = User.objects.create_user(username="u2", password="Old-pass-2026", role=User.Role.STOREKEEPER)
        c = self.client_class()
        token = c.post("/api/token/", {"username": "u2", "password": "Old-pass-2026"}, format="json").data["access"]
        r = self.client.post(f"{self.URL}{u.id}/set-password/", {"password": "New-pass-2027"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(c.get("/api/me/").status_code, 401)
        ok = self.client_class().post("/api/token/", {"username": "u2", "password": "New-pass-2027"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertNotIn("New-pass-2027", " ".join(AuditLog.objects.values_list("action", flat=True)))

    def test_disable_instead_of_delete(self):
        u = User.objects.create_user(username="u3", password="x", role=User.Role.STOREKEEPER)
        self.assertEqual(self.client.delete(f"{self.URL}{u.id}/").status_code, 405)
        self.assertEqual(self.client.patch(f"{self.URL}{u.id}/", {"is_active": False}, format="json").status_code, 200)
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        bad = self.client_class().post("/api/token/", {"username": "u3", "password": "x"}, format="json")
        self.assertEqual(bad.status_code, 401)

    def test_cannot_disable_self_or_last_admin(self):
        r = self.client.patch(f"{self.URL}{self.admin.id}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch(f"{self.URL}{self.admin.id}/", {"role": "STOREKEEPER"}, format="json")
        self.assertEqual(r.status_code, 400)
        other = User.objects.create_user(username="boss2", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(other)
        # Есть ещё один админ — первого отключить можно.
        r = self.client.patch(f"{self.URL}{self.admin.id}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.client.force_authenticate(self.admin)

    def test_password_cannot_be_patched_inline(self):
        u = User.objects.create_user(username="u4", password="x")
        r = self.client.patch(f"{self.URL}{u.id}/", {"password": "Qwerty-2026"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_only_admin_can_use_it(self):
        for role in (User.Role.STOREKEEPER, User.Role.ACCOUNTANT):
            user = User.objects.create_user(username=f"r_{role}", password="x", role=role)
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(self.URL).status_code, 403)
            self.assertEqual(self.client.post(self.URL, {}, format="json").status_code, 403)


class EmployeeTests(APITestCase):
    URL = "/api/staff/employees/"

    def setUp(self):
        self.admin = User.objects.create_user(username="boss", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def test_crud_and_user_link(self):
        user = User.objects.create_user(username="chpu", password="x")
        r = self.client.post(self.URL, {
            "full_name": "Азамат", "position": "Мастер", "default_machine": "CNC", "user": user.id,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["user_username"], "chpu")
        again = self.client.post(self.URL, {"full_name": "Другой", "user": user.id}, format="json")
        self.assertEqual(again.status_code, 400)           # учётка уже у Азамата
        r = self.client.patch(f"{self.URL}{r.data['id']}/", {"is_active": False}, format="json")
        self.assertFalse(r.data["is_active"])

    def test_employee_with_payroll_is_not_deleted(self):
        from datetime import date
        from decimal import Decimal

        from finance.models import PayrollAdjustment

        e = Employee.objects.create(full_name="Бакыт")
        PayrollAdjustment.objects.create(employee=e, month=date(2026, 10, 1), amount=Decimal("100"))
        r = self.client.delete(f"{self.URL}{e.id}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Employee.objects.filter(pk=e.pk).exists())
        free = Employee.objects.create(full_name="Свободный")
        self.assertEqual(self.client.delete(f"{self.URL}{free.id}/").status_code, 204)

    def test_accountant_reads_storekeeper_does_not(self):
        Employee.objects.create(full_name="Бакыт")
        acc = User.objects.create_user(username="acc", password="x", role=User.Role.ACCOUNTANT)
        self.client.force_authenticate(acc)
        self.assertEqual(self.client.get(self.URL).status_code, 200)
        self.assertEqual(self.client.post(self.URL, {"full_name": "X"}, format="json").status_code, 403)
        store = User.objects.create_user(username="st", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(store)
        self.assertEqual(self.client.get(self.URL).status_code, 403)
