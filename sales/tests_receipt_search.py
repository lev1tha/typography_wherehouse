"""Поиск чеков: цифры — это номер заказа.

«5» находило 242 чека из 280 (любое число с пятёркой внутри: номер, телефон,
название), а «№100» не находило ничего. Теперь значки «№»/«#» и пробелы
срезаются; до трёх цифр — точный номер; от четырёх — точный номер ИЛИ
вхождение в телефон; точное совпадение номера стоит первым.
"""
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt


class ReceiptSearchTests(APITestCase):
    URL = "/api/sales/receipts/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="rs_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.person = Client.objects.create(
            full_name="Иван Петров", phone="+996555123456"
        )
        for n in range(1, 31):
            Receipt.objects.create(order_number=n, title=f"Заказ {n}")
        Receipt.objects.create(order_number=100, title="Вывеска")
        # Старый чек №1234 и более новый чек другого клиента с «1234» в телефоне.
        Receipt.objects.create(order_number=1234, title="Баннер")
        other = Client.objects.create(full_name="Пётр", phone="+996551234777")
        Receipt.objects.create(order_number=31, client=other, title="Таблички")
        Receipt.objects.create(order_number=32, client=self.person, title="Визитки")

    def _numbers(self, query):
        resp = self.client.get(self.URL, {"search": query})
        self.assertEqual(resp.status_code, 200, resp.data)
        return [r["order_number"] for r in resp.data["results"]]

    def test_short_digits_are_an_exact_number(self):
        self.assertEqual(self._numbers("5"), [5])
        self.assertEqual(self._numbers("12"), [12])

    def test_number_sign_hash_and_spaces_are_stripped(self):
        self.assertEqual(self._numbers("№100"), [100])
        self.assertEqual(self._numbers("#100"), [100])
        self.assertEqual(self._numbers("  100 "), [100])
        self.assertEqual(self._numbers("№ 100"), [100])

    def test_four_digits_match_the_number_or_the_phone(self):
        found = self._numbers("1234")
        # +996551234777 (чек 31) и +996555123456 (чек 32) содержат «1234».
        self.assertEqual(set(found), {1234, 31, 32})
        # Точное совпадение номера — первым, хотя чеки 31 и 32 новее.
        self.assertEqual(found[0], 1234)

    def test_phone_tail_finds_the_clients_receipts(self):
        self.assertEqual(set(self._numbers("3456")), {32})

    def test_three_digits_do_not_match_a_phone(self):
        self.assertEqual(self._numbers("345"), [])

    def test_explicit_ordering_wins_over_exact_first(self):
        # №1234 делаем самым новым: по возрастанию даты он обязан быть последним.
        Receipt.objects.filter(order_number=1234).update(
            created_at=timezone.now() + timedelta(days=1)
        )
        resp = self.client.get(self.URL, {"search": "1234", "ordering": "created_at"})
        numbers = [r["order_number"] for r in resp.data["results"]]
        self.assertEqual(set(numbers), {1234, 31, 32})
        # Порядок задан явно (по дате заказа, старые первыми) — «точный первым»
        # не вмешивается.
        self.assertEqual(numbers[-1], 1234)
        created = list(
            Receipt.objects.filter(order_number__in=numbers)
            .order_by("created_at").values_list("order_number", flat=True)
        )
        self.assertEqual(numbers, created)

    def test_words_still_search_names_and_titles(self):
        self.assertEqual(self._numbers("Вывеска"), [100])
        self.assertEqual(set(self._numbers("Иван")), {32})

    def test_an_absurdly_long_number_is_just_no_match(self):
        self.assertEqual(self._numbers("99999999999999999999"), [])

    def test_stats_follow_the_same_search(self):
        resp = self.client.get(self.URL + "stats/", {"search": "№100"})
        self.assertEqual(resp.data["total"], 1)
