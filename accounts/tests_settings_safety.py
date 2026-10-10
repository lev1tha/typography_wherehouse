"""Логи 500-ок и небезопасные значения по умолчанию.

1. Без блока LOGGING при DEBUG=False трассировка ошибки не попадала никуда — в
   `docker logs` было пусто, и прод-пятисотку нельзя было разобрать.
2. Забытые переменные окружения (SECRET_KEY, FINANCE_PASSWORD, ALLOWED_HOSTS)
   система переживает молча. Старт ронять нельзя (прод может на них опираться),
   поэтому — громкий ERROR в лог и ошибки в `check --deploy`.
"""
import logging
from unittest import mock

from django.conf import settings
from django.core import checks
from django.test import Client as HttpClient
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.models import User
from config import checks as cloude_checks

GOOD = dict(
    DEBUG=False,
    SECRET_KEY="x" * 64,
    ALLOWED_HOSTS=["chpucenter.com"],
    FINANCE_PASSWORD="своё-длинное-слово",
    PAYMENT_GATEWAY="freedompay",
)


class LoggingTests(TestCase):
    def test_config_sends_warnings_and_errors_to_the_console(self):
        """Без LOGGING трассировка 500 при DEBUG=False не попадала никуда."""
        cfg = settings.LOGGING
        self.assertEqual(cfg["handlers"]["console"]["class"], "logging.StreamHandler")
        self.assertIn("console", cfg["root"]["handlers"])     # логгеры приложений
        self.assertIn("console", cfg["loggers"]["django"]["handlers"])   # django.request
        self.assertEqual(settings.LOG_LEVEL, "WARNING")

    def test_view_exception_is_logged_as_error_with_traceback(self):
        user = User.objects.create_user(username="log_admin", password="x", role=User.Role.ADMIN)
        http = HttpClient(raise_request_exception=False)
        http.force_login(user)
        with mock.patch("accounts.views.MeView.get", side_effect=RuntimeError("взрыв")):
            with self.assertLogs("django.request", level="ERROR") as logs:
                # force_login даёт сессию, DRF её не принимает — нужен токен.
                from rest_framework_simplejwt.tokens import AccessToken

                token = AccessToken.for_user(user)
                token["cv"] = 0
                r = http.get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(r.status_code, 500)
        record = logs.records[0]
        self.assertEqual(record.levelname, "ERROR")
        self.assertIsNotNone(record.exc_info)
        self.assertIs(record.exc_info[0], RuntimeError)


class InsecureDefaultsTests(SimpleTestCase):
    @override_settings(**GOOD)
    def test_clean_settings_give_no_problems(self):
        self.assertEqual(cloude_checks.insecure_defaults(), [])
        self.assertEqual(cloude_checks.check_insecure_defaults(None), [])

    @override_settings(
        **{**GOOD, "SECRET_KEY": "django-insecure-change-me-in-production",
           "ALLOWED_HOSTS": ["*"], "FINANCE_PASSWORD": "finance123"}
    )
    def test_defaults_are_reported_by_deploy_check(self):
        found = cloude_checks.check_insecure_defaults(None)
        ids = {m.id for m in found}
        self.assertEqual(ids, {"cloude.E001", "cloude.E002", "cloude.E003"})
        self.assertTrue(all(m.level == checks.ERROR for m in found))

    @override_settings(**{**GOOD, "PAYMENT_GATEWAY": "mock"})
    def test_mock_gateway_is_only_a_warning(self):
        found = cloude_checks.check_insecure_defaults(None)
        self.assertEqual([m.id for m in found], ["cloude.W004"])
        self.assertEqual(found[0].level, checks.WARNING)

    @override_settings(**{**GOOD, "FINANCE_PASSWORD": "finance123"})
    def test_startup_logs_loudly_but_does_not_raise(self):
        with self.assertLogs("config.security", level="ERROR") as logs:
            cloude_checks.log_insecure_defaults()   # не бросает: старт не роняем
        self.assertIn("FINANCE_PASSWORD", logs.output[0])

    @override_settings(**{**GOOD, "DEBUG": True, "FINANCE_PASSWORD": "finance123"})
    def test_debug_mode_stays_quiet(self):
        with self.assertNoLogs("config.security", level="WARNING"):
            cloude_checks.log_insecure_defaults()
