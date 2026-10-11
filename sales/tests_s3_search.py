"""S3 перепроверки владельца, XL-10: поиск чеков по размерам строк.

«0.33», «0,33» — метры, «330» — миллиметры, «330×370» — ширина и длина
детали (в любом порядке). Номер заказа по-прежнему ищется точно и стоит первым.
"""
from decimal import Decimal as D

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from services.models import PrintingService


class SizeSearchTests(APITestCase):
    URL = "/api/sales/receipts/"

    def setUp(self):
        self.admin = User.objects.create_user(username="ss_boss", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        cut = PrintingService.objects.create(name="Резка тест", kind="CUTTING", machine="CNC")

        def order(n, w=None, length=None, title=""):
            r = Receipt.objects.create(order_number=n, title=title)
            if w is not None:
                for _ in range(2):          # две строки — чек в выдаче один раз
                    TransactionItem.objects.create(
                        receipt=r, type="SERVICE", service=cut, quantity=D("1"),
                        price_per_item=D("10"), width=D(w), length=D(length),
                    )
            return r

        order(1, "0.33", "0.37")
        order(2, "0.37", "0.5")
        order(3, "1.22", "2.44")
        order(330, title="Номер 330")
        order(4, title="Табличка 0,33 по договорённости")

    def numbers(self, query):
        resp = self.client.get(self.URL, {"search": query})
        self.assertEqual(resp.status_code, 200, resp.data)
        return [r["order_number"] for r in resp.data["results"]]

    def test_metres_with_a_dot_or_a_comma(self):
        self.assertEqual(sorted(self.numbers("0.33")), [1])
        self.assertEqual(sorted(self.numbers("0,33")), [1, 4])        # и название с «0,33»
        self.assertEqual(sorted(self.numbers("0.37")), [1, 2])        # ширина или длина

    def test_millimetres_and_the_exact_number_first(self):
        self.assertEqual(self.numbers("330"), [330, 1])
        self.assertEqual(sorted(self.numbers("2440")), [3])

    def test_a_pair_of_sizes_in_any_order(self):
        self.assertEqual(self.numbers("330x370"), [1])
        self.assertEqual(self.numbers("0,37×0,33"), [1])
        self.assertEqual(self.numbers("1220 х 2440"), [3])             # кириллическая «х»
        self.assertEqual(self.numbers("0.33x0.5"), [])

    def test_plain_numbers_and_words_work_as_before(self):
        self.assertEqual(self.numbers("2"), [2])
        self.assertEqual(self.numbers("Табличка"), [4])
