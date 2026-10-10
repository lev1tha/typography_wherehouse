"""Лимит входа: обход через X-Forwarded-For, нормализация логина, только
неудачные попытки, форма входа Django-админки.

Базовые проверки предела (10 неудач в минуту) — в `tests_login_throttle.py`.
"""
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.throttling import account_key, client_ip
from clients.models import Client


def with_proxies(count):
    return override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": count})


class ClientIpTests(TestCase):
    def test_default_keeps_old_behaviour(self):
        """Без TRUSTED_PROXY_COUNT адрес берётся как раньше — целиком XFF."""
        meta = {"HTTP_X_FORWARDED_FOR": "1.1.1.1, 2.2.2.2", "REMOTE_ADDR": "9.9.9.9"}
        self.assertEqual(client_ip(meta), "1.1.1.1,2.2.2.2")
        self.assertEqual(client_ip({"REMOTE_ADDR": "9.9.9.9"}), "9.9.9.9")

    @with_proxies(1)
    def test_one_trusted_proxy_takes_the_last_address(self):
        meta = {"HTTP_X_FORWARDED_FOR": "6.6.6.6, 2.2.2.2", "REMOTE_ADDR": "9.9.9.9"}
        self.assertEqual(client_ip(meta), "2.2.2.2")

    def test_account_key_normalises(self):
        self.assertEqual(account_key("Лазер"), account_key("  лазер "))
        self.assertEqual(account_key("la zer"), account_key("LAZER"))
        self.assertEqual(
            account_key(phone="+996 555 77-78-88"), account_key(phone="0555777888")
        )
        self.assertIsNone(account_key(None, None))
        # Не строка не роняет вход пятисотой.
        self.assertIsNone(account_key(123, ["x"]))


class StaffLoginLimitTests(APITestCase):
    URL = "/api/token/"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="lim_admin", password="right-one", role=User.Role.ADMIN
        )

    def tearDown(self):
        cache.clear()

    def _try(self, password="wrong", username="lim_admin", **extra):
        return self.client.post(
            self.URL, {"username": username, "password": password}, format="json", **extra
        )

    def test_spoofed_xff_does_not_bypass_when_proxy_count_is_set(self):
        """С TRUSTED_PROXY_COUNT=1 приписанные слева адреса игнорируются."""
        with with_proxies(1):
            for i in range(10):
                r = self._try(username=f"nobody{i}", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}, 7.7.7.7")
                self.assertEqual(r.status_code, 401)
            blocked = self._try(username="nobody-x", HTTP_X_FORWARDED_FOR="10.0.0.99, 7.7.7.7")
            self.assertEqual(blocked.status_code, 429)
            # Другой настоящий клиент за тем же прокси — со своим счётчиком.
            other = self._try("right-one", HTTP_X_FORWARDED_FOR="10.0.0.1, 8.8.8.8")
            self.assertEqual(other.status_code, 200)

    def test_default_without_proxy_count_is_unchanged(self):
        """Переменная не задана — поведение прежнее (подпись XFF делит счётчик)."""
        for i in range(12):
            r = self._try(username=f"nobody{i}", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}")
            self.assertEqual(r.status_code, 401)  # у каждой подписи свой счётчик

    def test_login_variants_share_one_account_counter(self):
        """«Lim_Admin», « lim_admin » — один аккаунт: регистр и пробелы не дают
        чистого счётчика. Адреса разные (прокси), чтобы сработал именно лимит
        аккаунта (20/час)."""
        names = ["lim_admin", " lim_admin", "LIM_ADMIN", "Lim_Admin ", "lim_ admin"]
        with with_proxies(1):
            for i in range(20):
                r = self._try(username=names[i % len(names)],
                              HTTP_X_FORWARDED_FOR=f"10.0.1.{i}")
                self.assertEqual(r.status_code, 401, f"попытка {i + 1}")
            blocked = self._try(username="Lim_Admin", HTTP_X_FORWARDED_FOR="10.0.1.200")
            self.assertEqual(blocked.status_code, 429)

    def test_successful_logins_do_not_burn_the_limit(self):
        for i in range(15):
            self.assertEqual(self._try("right-one").status_code, 200, f"вход {i + 1}")

    def test_success_resets_the_account_counter(self):
        with with_proxies(1):
            for i in range(19):
                self._try(HTTP_X_FORWARDED_FOR=f"10.0.2.{i}")
            # 19 чужих неудач не мешают настоящему владельцу...
            self.assertEqual(
                self._try("right-one", HTTP_X_FORWARDED_FOR="10.0.2.100").status_code, 200
            )
            # ...а его вход обнулил счётчик: ещё 19 неудач снова не блокируют.
            for i in range(19):
                r = self._try(HTTP_X_FORWARDED_FOR=f"10.0.3.{i}")
                self.assertEqual(r.status_code, 401, f"попытка {i + 1}")

    def test_blocked_attempt_does_not_extend_the_address_counter(self):
        """Человек, которого заперли в чужой аккаунт, сам не должен запираться."""
        with with_proxies(1):
            for i in range(20):  # кто-то выбил лимит аккаунта с разных адресов
                self._try(HTTP_X_FORWARDED_FOR=f"10.0.4.{i}")
            for _ in range(5):
                self.assertEqual(
                    self._try(HTTP_X_FORWARDED_FOR="3.3.3.3").status_code, 429
                )
            User.objects.create_user(username="fine", password="pw-fine")
            ok = self._try("pw-fine", username="fine", HTTP_X_FORWARDED_FOR="3.3.3.3")
            self.assertEqual(ok.status_code, 200)

    def test_non_string_credentials_do_not_crash(self):
        r = self.client.post(self.URL, {"username": 123, "password": ["x"]}, format="json")
        self.assertIn(r.status_code, (400, 401))


class CustomerLoginLimitTests(APITestCase):
    URL = "/api/customer/login/"

    def setUp(self):
        cache.clear()
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555777888")
        self.customer.set_password("portal-pass")
        self.customer.save()

    def tearDown(self):
        cache.clear()

    def _post(self, **body):
        return self.client.post(self.URL, body, format="json")

    def test_two_step_login_is_not_counted_as_two_attempts(self):
        """Телефон, затем пароль — два запроса; под лимитом в 10 это должно
        давать десять входов, а не пять."""
        for i in range(12):
            first = self._post(phone="+996555777888")
            self.assertEqual(first.status_code, 200, f"шаг 1, вход {i + 1}")
            second = self._post(phone="+996555777888", password="portal-pass")
            self.assertEqual(second.status_code, 200, f"шаг 2, вход {i + 1}")

    def test_phone_spellings_share_one_account_counter(self):
        spellings = ["+996555777888", "0555777888", "555 77 78 88", "+996 (555) 777-888"]
        with with_proxies(1):
            for i in range(20):
                r = self.client.post(
                    self.URL,
                    {"phone": spellings[i % 4], "password": "nope"},
                    format="json",
                    HTTP_X_FORWARDED_FOR=f"10.1.0.{i}",
                )
                self.assertEqual(r.status_code, 400, f"попытка {i + 1}")
            blocked = self.client.post(
                self.URL,
                {"phone": "0555-777-888", "password": "nope"},
                format="json",
                HTTP_X_FORWARDED_FOR="10.1.0.250",
            )
            self.assertEqual(blocked.status_code, 429)


class DjangoAdminLoginLimitTests(TestCase):
    URL = "/django-admin/login/"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser(
            username="adm", password="right-one", role=User.Role.ADMIN
        )

    def tearDown(self):
        cache.clear()

    def _post(self, password="wrong", username="adm", **extra):
        return self.client.post(
            self.URL,
            {"username": username, "password": password, "next": "/django-admin/"},
            HTTP_HOST="localhost",
            **extra,
        )

    def test_guessing_is_cut_off_after_ten_failures(self):
        for i in range(10):
            self.assertEqual(self._post().status_code, 200, f"попытка {i + 1}")
        blocked = self._post()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Слишком много попыток", blocked.content.decode())
        self.assertEqual(self._post("right-one").status_code, 429)

    def test_successful_logins_do_not_burn_the_limit(self):
        for i in range(12):
            r = self._post("right-one")
            self.assertEqual(r.status_code, 302, f"вход {i + 1}")
            self.client.logout()

    def test_account_limit_applies_across_addresses(self):
        with with_proxies(1):
            for i in range(20):
                r = self._post(HTTP_X_FORWARDED_FOR=f"10.2.0.{i}")
                self.assertEqual(r.status_code, 200, f"попытка {i + 1}")
            blocked = self._post(username=" ADM", HTTP_X_FORWARDED_FOR="10.2.0.250")
            self.assertEqual(blocked.status_code, 429)

    def test_page_itself_is_not_limited(self):
        for _ in range(15):
            self.assertEqual(self.client.get(self.URL, HTTP_HOST="localhost").status_code, 200)
