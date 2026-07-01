"""Клиентский портал: вход по телефону + собственному паролю.

Раньше вход был только по телефону — кто знал чужой номер, видел чужие заказы.
Теперь при первом входе клиент задаёт себе пароль, дальше входит с ним; персонал
может сбросить пароль (клиент забыл).
"""
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client

LOGIN = "/api/customer/login/"


class CustomerPasswordFlowTests(APITestCase):
    def setUp(self):
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Айбек", phone="+996700111222"
        )
        self.other = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Чужой", phone="+996700333444"
        )
        self.staff = User.objects.create_user(
            username="store_pw", password="x", role=User.Role.STOREKEEPER
        )

    def _login(self, phone, password=None):
        body = {"phone": phone}
        if password is not None:
            body["password"] = password
        return self.client.post(LOGIN, body, format="json")

    # ---- первый вход: задать пароль ----------------------------------------
    def test_first_login_asks_to_set_password(self):
        r = self._login("+996700111222")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "set_password")
        self.assertEqual(r.data["name"], "Айбек")
        self.assertNotIn("access", r.data)

    def test_setting_password_logs_in_and_hashes(self):
        r = self._login("+996700111222", "sekret1")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)
        self.assertEqual(r.data["client"]["id"], self.customer.id)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.has_password)
        # Пароль хранится только как хеш, не в открытом виде.
        self.assertNotEqual(self.customer.portal_password, "sekret1")
        self.assertTrue(self.customer.check_password("sekret1"))

    def test_password_too_short_rejected(self):
        r = self._login("+996700111222", "12")
        self.assertEqual(r.status_code, 400, r.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.has_password)

    # ---- повторный вход: ввести пароль -------------------------------------
    def test_returning_login_asks_for_password(self):
        self.customer.set_password("sekret1")
        self.customer.save()
        r = self._login("+996700111222")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "need_password")
        self.assertNotIn("access", r.data)

    def test_login_with_correct_password(self):
        self.customer.set_password("sekret1")
        self.customer.save()
        r = self._login("+996700111222", "sekret1")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)

    def test_login_with_wrong_password_rejected(self):
        self.customer.set_password("sekret1")
        self.customer.save()
        r = self._login("+996700111222", "guess")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertNotIn("access", r.data)

    # ---- ключевой сценарий: чужой номер без пароля не откроет заказы --------
    def test_knowing_someones_phone_is_not_enough(self):
        # У клиента уже есть пароль. Злоумышленник знает телефон, но не пароль.
        self.customer.set_password("realpass")
        self.customer.save()
        # Шаг 1: просит пароль (не выдаёт токен по одному телефону).
        step1 = self._login("+996700111222")
        self.assertEqual(step1.data.get("status"), "need_password")
        self.assertNotIn("access", step1.data)
        # Шаг 2: подбор — отказ.
        step2 = self._login("+996700111222", "123456")
        self.assertEqual(step2.status_code, 400, step2.data)

    # ---- сброс пароля персоналом -------------------------------------------
    def test_staff_reset_password_allows_new_setup(self):
        self.customer.set_password("oldpass")
        self.customer.save()
        self.client.force_authenticate(self.staff)
        r = self.client.post(f"/api/clients/clients/{self.customer.id}/reset-password/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.has_password)
        # После сброса вход снова предлагает задать пароль.
        self.client.force_authenticate(None)
        step = self._login("+996700111222")
        self.assertEqual(step.data.get("status"), "set_password")

    def test_reset_password_requires_staff_auth(self):
        r = self.client.post(f"/api/clients/clients/{self.customer.id}/reset-password/", {}, format="json")
        self.assertIn(r.status_code, (401, 403))

    # ---- has_password в сериализаторе --------------------------------------
    def test_serializer_exposes_has_password(self):
        self.client.force_authenticate(self.staff)
        r = self.client.get(f"/api/clients/clients/{self.customer.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("has_password", r.data)
        self.assertFalse(r.data["has_password"])
