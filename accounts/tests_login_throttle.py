"""Пароли нельзя подбирать перебором.

Две двери системы открыты без токена — вход сотрудника и вход в кабинет
клиента. Предела попыток у них не было вовсе: пароль кабинета выдаёт админ, он
короткий, а портал живёт на публичном домене.

Считаем в двух разрезах: по адресу (обычный перебор с одной машины) и по самому
логину (перебор одного аккаунта с разных адресов).
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client


class StaffLoginThrottleTests(APITestCase):
    URL = "/api/token/"

    def setUp(self):
        cache.clear()   # счётчики живут в кеше и текут между тестами
        self.user = User.objects.create_user(
            username="thr_admin", password="right-one", role=User.Role.ADMIN
        )

    def tearDown(self):
        cache.clear()

    def _try(self, password="wrong", username="thr_admin", **extra):
        return self.client.post(
            self.URL, {"username": username, "password": password}, format="json", **extra
        )

    def test_correct_password_still_works(self):
        self.assertEqual(self._try("right-one").status_code, 200)

    def test_guessing_is_cut_off_after_ten_tries(self):
        for i in range(10):
            self.assertEqual(self._try().status_code, 401, f"попытка {i + 1}")
        blocked = self._try()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("слишком много попыток", str(blocked.data).lower())
        # И правильный пароль тоже подождёт: иначе предел обходится за одну
        # верную догадку.
        self.assertEqual(self._try("right-one").status_code, 429)

    def test_another_account_from_the_same_address_shares_the_address_limit(self):
        """Перебор «по словарю логинов» с одной машины — тот же перебор."""
        User.objects.create_user(username="thr_other", password="x", role=User.Role.ADMIN)
        for _ in range(10):
            self._try(username="thr_admin")
        self.assertEqual(self._try(username="thr_other").status_code, 429)


class CustomerLoginThrottleTests(APITestCase):
    URL = "/api/customer/login/"

    def setUp(self):
        cache.clear()
        self.client_obj = Client.objects.create(
            full_name="Тахир", phone="+996555777888"
        )
        self.client_obj.set_password("portal-pass")
        self.client_obj.save()

    def tearDown(self):
        cache.clear()

    def _try(self, password="nope"):
        return self.client.post(
            self.URL, {"phone": "+996555777888", "password": password}, format="json"
        )

    def test_portal_password_cannot_be_brute_forced(self):
        for i in range(10):
            self.assertEqual(self._try().status_code, 400, f"попытка {i + 1}")
        blocked = self._try()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("попробуйте через", str(blocked.data).lower())

    def test_staff_login_is_not_blocked_by_customer_attempts(self):
        """У кабинета свой счётчик: перебор клиентских паролей не должен
        запирать кассу."""
        User.objects.create_user(username="thr_cash", password="right-one", role=User.Role.STOREKEEPER)
        for _ in range(10):
            self._try()
        staff = self.client.post(
            "/api/token/", {"username": "thr_cash", "password": "right-one"}, format="json"
        )
        self.assertEqual(staff.status_code, 200, staff.data)
