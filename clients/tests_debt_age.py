"""Дебиторка: возраст долга, сальдо, корзины, «спящие», маржа (CLI-01, cash-08, CLI-10).

Сценарий владельца: ОсОО «Ак Жол» должно за три заказа (14 000 — 100 дней
назад, 4 763 — 45 дней, 6 200 — 2 дня), физлицо Бакыт — 1 200 за 70 дней.
Excel даёт корзины 0–30 = 6 200, 31–60 = 4 763, 61–90 = 1 200, >90 = 14 000.
"""
from datetime import timedelta

from django.utils import timezone

from clients.models import Client
from clients.testkit import D, ShopCase


class DebtAgeTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.bakyt = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Бакыт Осмонов", phone="+996700123456",
        )
        self.sale(24000, paid=10000, days_ago=100)
        self.sale(4763, paid=0, days_ago=45)
        self.sale(8500, paid=8500, days_ago=5)
        self.sale(6200, paid=0, days_ago=2)
        self.sale(1200, client=self.bakyt, paid=0, days_ago=70)

    def rows(self, **params):
        r = self.client.get("/api/clients/clients/", params)
        self.assertEqual(r.status_code, 200, r.data)
        return {row["display_name"]: row for row in r.data["results"]}

    def names(self, **params):
        r = self.client.get("/api/clients/clients/", params)
        self.assertEqual(r.status_code, 200, r.data)
        return [row["display_name"] for row in r.data["results"]]

    def test_list_shows_oldest_debt_and_days(self):
        row = self.rows()["ОсОО «Ак Жол»"]
        self.assertEqual(D(str(row["debt"])), D("24963"))
        self.assertEqual(row["overdue_days"], 100)
        self.assertEqual(
            row["oldest_debt_at"], (timezone.localdate() - timedelta(days=100)).isoformat()
        )
        self.assertEqual(self.rows()["Бакыт Осмонов"]["overdue_days"], 70)

    def test_client_without_debt_has_no_age(self):
        paid = Client.objects.create(full_name="Платит сразу", phone="+996700000009")
        self.sale(500, client=paid, paid=500, days_ago=10)
        row = self.rows()["Платит сразу"]
        self.assertIsNone(row["oldest_debt_at"])
        self.assertIsNone(row["overdue_days"])
        self.assertEqual(row["days_since_last_order"], 10)

    def test_filter_overdue_days(self):
        self.assertEqual(set(self.names(overdue_days=30)), {"ОсОО «Ак Жол»", "Бакыт Осмонов"})
        self.assertEqual(self.names(overdue_days=90), ["ОсОО «Ак Жол»"])
        self.assertEqual(self.names(overdue_days=101), [])
        # мусор в параметре не роняет список и не фильтрует
        self.assertEqual(len(self.names(overdue_days="abc")), 2)

    def test_sort_by_age(self):
        self.assertEqual(self.names(ordering="-overdue_days"), ["ОсОО «Ак Жол»", "Бакыт Осмонов"])
        self.assertEqual(self.names(ordering="overdue_days"), ["Бакыт Осмонов", "ОсОО «Ак Жол»"])
        self.assertEqual(self.names(ordering="oldest_debt"), ["ОсОО «Ак Жол»", "Бакыт Осмонов"])

    def test_sort_puts_clients_without_debt_last(self):
        Client.objects.create(full_name="Без долга", phone="+996700000008")
        self.assertEqual(self.names(ordering="-overdue_days")[-1], "Без долга")
        self.assertEqual(self.names(ordering="overdue_days")[-1], "Без долга")

    def test_bucket_filter_matches_clients_with_debt_in_range(self):
        self.assertEqual(self.names(age_from=31, age_to=60), ["ОсОО «Ак Жол»"])      # 45 дней
        self.assertEqual(self.names(age_from=61, age_to=90), ["Бакыт Осмонов"])      # 70 дней
        self.assertEqual(self.names(age_from=91), ["ОсОО «Ак Жол»"])                 # 100 дней
        self.assertEqual(self.names(age_from=0, age_to=30), ["ОсОО «Ак Жол»"])       # 2 дня

    def test_aging_tiles_match_excel(self):
        r = self.client.get("/api/clients/clients/aging/")
        self.assertEqual(r.status_code, 200, r.data)
        got = {b["key"]: (D(str(b["amount"])), b["orders"], b["clients"]) for b in r.data["buckets"]}
        self.assertEqual(got["0_30"], (D("6200"), 1, 1))
        self.assertEqual(got["31_60"], (D("4763"), 1, 1))
        self.assertEqual(got["61_90"], (D("1200"), 1, 1))
        self.assertEqual(got["90_plus"], (D("14000"), 1, 1))
        self.assertEqual(D(str(r.data["total"])), D("26163"))

    def test_aging_total_equals_sum_of_client_debts(self):
        # долг «с улицы» — без клиента — тоже в корзинах и в строке no_client
        self.sale(900, client=None, paid=0, days_ago=3) if False else None
        from sales import sale_service
        sale_service.create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.coin, "quantity": D("900"), "mode": "PIECE"}],
            amount_paid=D("0"),
        )
        r = self.client.get("/api/clients/clients/aging/")
        listed = sum((D(str(x["debt"])) for x in self.client.get("/api/clients/clients/").data["results"]), D("0"))
        self.assertEqual(D(str(r.data["total"])), listed + D("900"))
        self.assertEqual(D(str(r.data["no_client"]["amount"])), D("900"))

    def test_unrecognized_online_invoice_is_not_in_aging(self):
        self.sale(5000, method="ONLINE", paid=None, days_ago=200)
        r = self.client.get("/api/clients/clients/aging/")
        self.assertEqual(D(str(r.data["total"])), D("26163"))

    def test_sleeping_filter(self):
        quiet = Client.objects.create(full_name="Пропал", phone="+996700000007")
        self.sale(300, client=quiet, paid=300, days_ago=80)
        fresh = Client.objects.create(full_name="Свежий", phone="+996700000006")
        self.sale(300, client=fresh, paid=300, days_ago=3)
        never = Client.objects.create(full_name="Не заказывал", phone="+996700000005")
        sleepers = self.names(sleeping_days=60)
        self.assertIn("Пропал", sleepers)
        self.assertNotIn("Свежий", sleepers)
        self.assertNotIn(never.full_name, sleepers)   # заказов не было — не «спящий»
        # Ак Жол заказывал 2 дня назад, Бакыт — 70 дней назад
        self.assertIn("Бакыт Осмонов", sleepers)
        self.assertNotIn("ОсОО «Ак Жол»", sleepers)

    def test_last_order_date_and_sorting(self):
        row = self.rows()["Бакыт Осмонов"]
        self.assertEqual(row["days_since_last_order"], 70)
        self.assertEqual(self.names(ordering="-last_order_at")[0], "ОсОО «Ак Жол»")

    def test_balance_is_debt_minus_change(self):
        taher = Client.objects.create(full_name="Тахир", phone="+996700000004")
        self.sale(1500, client=taher, paid=3100, days_ago=20)      # сдача 1 600
        self.sale(16800, client=taher, paid=0, days_ago=10)        # долг 16 800
        row = self.rows()["Тахир"]
        self.assertEqual(D(str(row["debt"])), D("16800"))
        self.assertEqual(D(str(row["change_due"])), D("1600"))
        self.assertEqual(D(str(row["balance"])), D("15200"))
        self.assertEqual(row["overdue_days"], 10)
        card = self.card(taher)
        self.assertEqual(D(str(card["balance"])), D("15200"))
        self.assertEqual(card["overdue_days"], 10)

    def test_balance_sorting(self):
        taher = Client.objects.create(full_name="Тахир", phone="+996700000004")
        self.sale(5000, client=taher, paid=9000, days_ago=20)      # аванс 4 000 → сальдо −4 000
        self.assertEqual(self.names(ordering="balance")[0], "Тахир")
        self.assertEqual(self.names(ordering="-balance")[0], "ОсОО «Ак Жол»")


class MarginTests(ShopCase):
    def setUp(self):
        super().setUp()
        # цена 1, закуп 0,4: продажа на 1 000 → маржа 600
        self.sale(1000, paid=1000, days_ago=30)
        self.sale(500, paid=500, days_ago=5)

    def test_admin_and_accountant_see_margin(self):
        for user in (self.admin, self.acc):
            self.client.force_authenticate(user)
            row = self.client.get("/api/clients/clients/").data["results"][0]
            self.assertEqual(D(str(row["margin"])), D("900"))      # 1500 − 0,4 × 1500
        card = self.card()
        self.assertEqual(D(str(card["margin"])), D("900"))

    def test_storekeeper_does_not_see_margin(self):
        self.client.force_authenticate(self.store)
        row = self.client.get("/api/clients/clients/").data["results"][0]
        self.assertNotIn("margin", row)
        self.assertNotIn("margin", self.client.get(f"/api/clients/clients/{self.agency.id}/").data)
        # и не может отсортировать по ней
        r = self.client.get("/api/clients/clients/", {"ordering": "margin_total"})
        self.assertEqual(r.status_code, 200)

    def test_margin_subtracts_refund(self):
        from sales import sale_service
        r = self.sale(200, paid=200, days_ago=1)
        sale_service.refund_receipt(r, user=self.admin)
        card = self.card()
        self.assertEqual(D(str(card["margin"])), D("900"))


class QueryBudgetTests(ShopCase):
    """Список клиентов не должен делать запрос на каждую строку."""

    def test_list_is_not_n_plus_one(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                r = self.client.get("/api/clients/clients/", {"page_size": 50})
            self.assertEqual(r.status_code, 200)
            return len(ctx)

        for i in range(3):
            c = Client.objects.create(full_name=f"К{i}", phone=f"+9967000001{i:02d}")
            self.sale(100, client=c, paid=0, days_ago=i)
        few = count_queries()
        for i in range(3, 30):
            c = Client.objects.create(full_name=f"К{i}", phone=f"+9967000001{i:02d}")
            self.sale(100, client=c, paid=0, days_ago=i)
        many = count_queries()
        self.assertLessEqual(many, few + 2, (few, many))
