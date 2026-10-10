"""Фильтр периода в списке чеков режет по границам МЕСТНЫХ суток.

Вместо `created_at__date` (база приводит к дате каждую строку, индекс молчит)
— диапазон `[начало дня, начало следующего)`. Набор чеков тот же: 23:30 по
Бишкеку принадлежит этому дню, хотя по UTC это 17:30, а 00:30 — уже следующему.
"""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt

BISHKEK = ZoneInfo("Asia/Bishkek")


def at(day, hh, mm):
    return datetime.combine(day, time(hh, mm), tzinfo=BISHKEK)


class ReceiptDateRangeTests(APITestCase):
    URL = "/api/sales/receipts/"

    def setUp(self):
        admin = User.objects.create_user(username="rd_boss", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(admin)
        d = date(2026, 10, 5)
        for number, moment in [
            (1, at(date(2026, 10, 4), 23, 59)),
            (2, at(d, 0, 0)),
            (3, at(d, 12, 0)),
            (4, at(d, 23, 59)),
            (5, at(date(2026, 10, 6), 0, 0)),
        ]:
            Receipt.objects.create(order_number=number, created_at=moment)

    def _numbers(self, **params):
        resp = self.client.get(self.URL, params)
        return sorted(r["order_number"] for r in resp.data["results"])

    def test_one_local_day(self):
        self.assertEqual(self._numbers(date_from="2026-10-05", date_to="2026-10-05"), [2, 3, 4])

    def test_open_ended_ranges(self):
        self.assertEqual(self._numbers(date_from="2026-10-05"), [2, 3, 4, 5])
        self.assertEqual(self._numbers(date_to="2026-10-04"), [1])

    def test_bad_dates_mean_no_filter(self):
        self.assertEqual(self._numbers(date_from="вчера"), [1, 2, 3, 4, 5])
