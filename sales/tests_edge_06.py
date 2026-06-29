from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from clients.serializers import ClientSerializer, client_debt
from sales.models import Receipt


class EdgeClientsTests(APITestCase):
    """Edge-cases: поиск (кириллица/регистр/телефон/пустой), client_debt,
    рефералы (самореферал, дубль-резолв, локед-реферал)."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="edge_admin", password="x", role=User.Role.ADMIN
        )
        self.store = User.objects.create_user(
            username="edge_store", password="x", role=User.Role.STOREKEEPER
        )
        # Клиент с заглавной кириллицей в ФИО.
        self.alice = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Тестовый Клиент", phone="+70077123"
        )
        # Клиент с нижним регистром.
        self.lower = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="алиса", phone="+70066001"
        )
        # ОСОО без full_name — проверка (c.full_name or '').lower() на None.
        self.company = Client.objects.create(
            type=Client.Type.OSOO, company_name="Ромашка ОСОО", phone="+70055002"
        )
        # Аутентифицируемся складовщиком по умолчанию (поиск доступен всем authd).
        self.client.force_authenticate(self.store)

    # ---------- ПОИСК ----------

    def _ids(self, resp):
        data = resp.data
        results = data["results"] if isinstance(data, dict) and "results" in data else data
        return {row["id"] for row in results}

    def test_search_cyrillic_lowercase_finds_uppercase(self):
        r = self.client.get("/api/clients/clients/", {"search": "тес"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.alice.id, self._ids(r))

    def test_search_cyrillic_uppercase_finds_lowercase(self):
        r = self.client.get("/api/clients/clients/", {"search": "АЛИ"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.lower.id, self._ids(r))

    def test_search_by_phone_substring(self):
        r = self.client.get("/api/clients/clients/", {"search": "70077"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.alice.id, self._ids(r))

    def test_search_by_company_name_with_null_full_name(self):
        # Не должно падать на None full_name у ОСОО.
        r = self.client.get("/api/clients/clients/", {"search": "ромаш"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.company.id, self._ids(r))

    def test_empty_search_returns_all(self):
        r = self.client.get("/api/clients/clients/", {"search": ""})
        self.assertEqual(r.status_code, 200)
        ids = self._ids(r)
        self.assertEqual(
            {self.alice.id, self.lower.id, self.company.id} & ids,
            {self.alice.id, self.lower.id, self.company.id},
        )

    def test_whitespace_search_returns_all(self):
        r = self.client.get("/api/clients/clients/", {"search": "   "})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.alice.id, self._ids(r))
        self.assertIn(self.company.id, self._ids(r))

    def test_search_no_match_returns_empty(self):
        r = self.client.get("/api/clients/clients/", {"search": "zzzнетищ"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._ids(r), set())

    def test_duplicate_phone_resolved_inline_via_search(self):
        # Касса вводит полный телефон существующего клиента — поиск отдаёт его,
        # чтобы зарезолвить инлайн, а не пытаться создать дубль (что дало бы 400).
        r = self.client.get("/api/clients/clients/", {"search": "+70077123"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.alice.id, self._ids(r))

    # ---------- client_debt ----------

    def _serialize(self, client):
        return ClientSerializer(client).data

    def test_debt_sums_pending_receipts(self):
        Receipt.objects.create(
            client=self.alice, total_price=Decimal("100"),
            payment_status=Receipt.PaymentStatus.PENDING,
        )
        Receipt.objects.create(
            client=self.alice, total_price=Decimal("50"),
            payment_status=Receipt.PaymentStatus.PENDING,
        )
        self.assertEqual(client_debt(self.alice), Decimal("150"))
        self.assertEqual(Decimal(str(self._serialize(self.alice)["debt"])), Decimal("150"))

    def test_debt_excludes_paid_cancelled_and_counts_prepay(self):
        # PENDING с предоплатой: долг 70.
        Receipt.objects.create(
            client=self.alice, total_price=Decimal("100"),
            amount_paid=Decimal("30"),
            payment_status=Receipt.PaymentStatus.PENDING,
        )
        # PAID: долг 0.
        Receipt.objects.create(
            client=self.alice, total_price=Decimal("200"),
            payment_status=Receipt.PaymentStatus.PAID,
        )
        # CANCELLED: долг 0.
        Receipt.objects.create(
            client=self.alice, total_price=Decimal("500"),
            payment_status=Receipt.PaymentStatus.PENDING,
            status=Receipt.Status.CANCELLED,
        )
        # PENDING, но полностью возвращён: долг 0.
        Receipt.objects.create(
            client=self.alice, total_price=Decimal("100"),
            refunded_amount=Decimal("100"),
            payment_status=Receipt.PaymentStatus.PENDING,
        )
        self.assertEqual(client_debt(self.alice), Decimal("70"))

    def test_debt_zero_without_receipts(self):
        self.assertEqual(client_debt(self.company), Decimal("0"))
        self.assertEqual(Decimal(str(self._serialize(self.company)["debt"])), Decimal("0"))

    # ---------- request-referral-change endpoint ----------

    def test_request_referral_change_self_referral_400(self):
        r = self.client.post(
            f"/api/clients/clients/{self.alice.id}/request-referral-change/",
            {"referred_by": self.alice.id}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("referred_by", r.data)

    def test_request_referral_change_same_referrer_400(self):
        self.lower.referred_by = self.alice
        self.lower.save()
        r = self.client.post(
            f"/api/clients/clients/{self.lower.id}/request-referral-change/",
            {"referred_by": self.alice.id}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("referred_by", r.data)

    def test_request_referral_change_unknown_referrer_400(self):
        r = self.client.post(
            f"/api/clients/clients/{self.lower.id}/request-referral-change/",
            {"referred_by": 999999}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("referred_by", r.data)

    # ---------- locked referral via PATCH ----------

    def test_storekeeper_cannot_change_locked_referral(self):
        self.lower.referred_by = self.alice
        self.lower.save()
        r = self.client.patch(
            f"/api/clients/clients/{self.lower.id}/",
            {"referred_by": self.company.id}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("referred_by", r.data)
        self.lower.refresh_from_db()
        self.assertEqual(self.lower.referred_by_id, self.alice.id)

    def test_storekeeper_patch_same_referral_value_ok(self):
        self.lower.referred_by = self.alice
        self.lower.save()
        r = self.client.patch(
            f"/api/clients/clients/{self.lower.id}/",
            {"referred_by": self.alice.id}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.lower.refresh_from_db()
        self.assertEqual(self.lower.referred_by_id, self.alice.id)
