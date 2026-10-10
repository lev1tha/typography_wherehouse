"""Время ответа входа клиента не выдаёт, выдан ли номеру пароль.

PBKDF2 (~0,3 с) выполнялся только у клиента с паролем; у неизвестного номера и
у клиента без пароля ответ приходил за ~0,002 с. По времени можно было
отличить «номер есть и пароль выдан» от остальных. Теперь на этих ветках
выполняется холостая сверка с постоянным хешем. Проверяем сам факт сверки, а не
секундомер.
"""
from unittest import mock

from django.core.cache import cache
from rest_framework.test import APITestCase

from clients.models import Client

LOGIN = "/api/customer/login/"
TEXT = "Неверный номер или пароль."


class LoginTimingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.with_pw = Client.objects.create(full_name="С паролем", phone="+996700111111")
        self.with_pw.set_password("issued99")
        self.with_pw.save()
        self.no_pw = Client.objects.create(full_name="Без пароля", phone="+996700222222")

    def tearDown(self):
        cache.clear()

    def _login(self, phone, password):
        return self.client.post(LOGIN, {"phone": phone, "password": password}, format="json")

    def test_unknown_phone_runs_a_dummy_check(self):
        with mock.patch("clients.customer.check_password") as dummy:
            r = self._login("+996700999999", "whatever")
        self.assertEqual(dummy.call_count, 1)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["detail"], TEXT)

    def test_client_without_password_runs_a_dummy_check(self):
        with mock.patch("clients.customer.check_password") as dummy:
            r = self._login("+996700222222", "whatever")
        self.assertEqual(dummy.call_count, 1)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["detail"], TEXT)

    def test_client_with_password_does_the_real_check_only(self):
        with mock.patch("clients.customer.check_password") as dummy:
            wrong = self._login("+996700111111", "bad")
            right = self._login("+996700111111", "issued99")
        self.assertEqual(dummy.call_count, 0)   # настоящая сверка — в Client.check_password
        self.assertEqual(wrong.data["detail"], TEXT)
        self.assertEqual(right.status_code, 200)

    def test_garbage_in_fields_is_a_400_not_a_crash(self):
        r = self.client.post(LOGIN, {"phone": 996700111111, "password": ["x"]}, format="json")
        self.assertEqual(r.status_code, 400)
