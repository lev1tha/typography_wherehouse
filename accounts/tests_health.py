"""GET /api/health/ — проверка живости для docker healthcheck."""
from unittest import mock

from django.db import OperationalError
from rest_framework.test import APITestCase


class HealthTests(APITestCase):
    URL = "/api/health/"

    def test_ok_without_authentication(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_not_throttled(self):
        for _ in range(60):
            self.assertEqual(self.client.get(self.URL).status_code, 200)

    def test_db_down_is_503_without_details(self):
        with mock.patch(
            "accounts.views.connection.cursor",
            side_effect=OperationalError("password authentication failed for user chpu"),
        ):
            with self.assertLogs("accounts.views", level="ERROR"), \
                    self.assertLogs("django.request", level="ERROR"):
                r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"status": "db_unavailable"})
        self.assertNotIn("chpu", r.content.decode())

    def test_stale_or_garbage_token_does_not_break_it(self):
        r = self.client.get(self.URL, HTTP_AUTHORIZATION="Bearer garbage")
        self.assertEqual(r.status_code, 200)
