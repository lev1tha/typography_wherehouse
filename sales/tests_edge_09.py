"""Edge-case tests for permissions & role isolation ("Права и изоляция ролей").

Covers, with self-contained setUp (no fixtures):
  - IsAdmin gate on warehouse supply/adjust/receive-roll/write-off (storekeeper -> 403)
  - cross-scope JWT: customer token on staff endpoints -> 401; staff token on
    /api/customer/* -> 401; deleted-client customer token -> 401
  - storekeeper sees only own receipts (list filtered, foreign detail -> 404)
  - admin sees all receipts
  - customer sees only own orders
  - customer login by missing / empty / differently-formatted phone

Staff auth is exercised with a REAL minted JWT (not force_authenticate) wherever
the test is specifically about token scope, so the cross-scope behaviour is what
is actually verified; pure-permission tests use force_authenticate for brevity.
"""
from decimal import Decimal

from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from clients.customer import mint_customer_token
from clients.models import Client
from sales.models import Receipt
from warehouse.models import Material


class EdgeRolesTests(APITestCase):
    def setUp(self):
        # Staff users.
        self.admin = User.objects.create_user(
            username="edge_admin", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="edge_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.keeper2 = User.objects.create_user(
            username="edge_keeper2", password="x", role=User.Role.STOREKEEPER
        )

        # A non-roll material for supply/adjust/write-off payloads.
        self.material = Material.objects.create(
            name="Бумага", category="Бумага", unit="PIECE",
            quantity=Decimal("50"), price_per_unit=Decimal("10"),
            purchase_price=Decimal("5"),
        )

        # Clients (portal customers).
        self.client_a = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Клиент А", phone="+996700000001",
        )
        self.client_b = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Клиент Б", phone="+996700000002",
        )

        # Receipts owned by different cashiers / clients.
        self.receipt_keeper = Receipt.objects.create(
            cashier=self.keeper, client=self.client_a, total_price=Decimal("100"),
        )
        self.receipt_keeper2 = Receipt.objects.create(
            cashier=self.keeper2, client=self.client_b, total_price=Decimal("200"),
        )

    # ---- helpers --------------------------------------------------------
    def _staff_bearer(self, user):
        """Real staff access token (default JWTAuthentication, has user_id)."""
        return f"Bearer {RefreshToken.for_user(user).access_token}"

    def _auth(self, header):
        self.client.credentials(HTTP_AUTHORIZATION=header)

    def _clear_auth(self):
        self.client.credentials()
        self.client.force_authenticate(user=None)

    # ---- IsAdmin gate on warehouse write-actions ------------------------
    def test_storekeeper_supply_forbidden(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.post(
            "/api/warehouse/materials/supply/",
            {"material": self.material.id, "quantity": "5"}, format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)

    def test_storekeeper_adjust_forbidden(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.post(
            "/api/warehouse/materials/adjust/",
            {"material": self.material.id, "counted_quantity": "40"}, format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)

    def test_storekeeper_receive_roll_forbidden(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.post(
            "/api/warehouse/materials/receive-roll/",
            {
                "material": self.material.id, "form": "ROLL",
                "width": "1.5", "length": "50", "purchase_cost": "1000",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)

    def test_storekeeper_write_off_forbidden(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.post(
            "/api/warehouse/materials/write-off/",
            {"material": self.material.id, "quantity": "3"}, format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)

    def test_admin_supply_not_forbidden(self):
        # Positive control: admin must pass the permission gate (so the 403
        # tests above prove a permission denial, not a broken payload).
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            "/api/warehouse/materials/supply/",
            {"material": self.material.id, "quantity": "5"}, format="json",
        )
        self.assertNotEqual(r.status_code, 403, r.data)
        self.assertEqual(r.status_code, 200, r.data)

    # ---- cross-scope JWT: customer token on staff endpoints -------------
    def test_customer_token_rejected_on_sales_endpoint(self):
        self._clear_auth()
        self._auth(f"Bearer {mint_customer_token(self.client_a)}")
        r = self.client.get("/api/sales/receipts/")
        self.assertEqual(r.status_code, 401, getattr(r, "data", None))

    def test_customer_token_rejected_on_warehouse_endpoint(self):
        self._clear_auth()
        self._auth(f"Bearer {mint_customer_token(self.client_a)}")
        r = self.client.get("/api/warehouse/materials/")
        self.assertEqual(r.status_code, 401, getattr(r, "data", None))

    # ---- cross-scope JWT: staff token on customer endpoint --------------
    def test_staff_token_rejected_on_customer_orders(self):
        self._clear_auth()
        self._auth(self._staff_bearer(self.keeper))
        r = self.client.get("/api/customer/orders/")
        self.assertEqual(r.status_code, 401, getattr(r, "data", None))

    def test_no_token_rejected_on_customer_orders(self):
        self._clear_auth()
        r = self.client.get("/api/customer/orders/")
        self.assertEqual(r.status_code, 401, getattr(r, "data", None))

    def test_customer_token_for_deleted_client_rejected(self):
        token = mint_customer_token(self.client_b)
        self.client_b.receipts.all().delete()
        self.client_b.delete()
        self._clear_auth()
        self._auth(f"Bearer {token}")
        r = self.client.get("/api/customer/orders/")
        self.assertEqual(r.status_code, 401, getattr(r, "data", None))

    # ---- receipt isolation: storekeeper sees only own ------------------
    def test_storekeeper_sees_only_own_receipts(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.get("/api/sales/receipts/")
        self.assertEqual(r.status_code, 200, r.data)
        results = r.data["results"] if isinstance(r.data, dict) and "results" in r.data else r.data
        ids = {str(row["id"]) for row in results}
        self.assertIn(str(self.receipt_keeper.id), ids)
        self.assertNotIn(str(self.receipt_keeper2.id), ids)

    def test_storekeeper_cannot_open_foreign_receipt(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.get(f"/api/sales/receipts/{self.receipt_keeper2.id}/")
        self.assertEqual(r.status_code, 404, getattr(r, "data", None))

    def test_admin_sees_all_receipts(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/sales/receipts/")
        self.assertEqual(r.status_code, 200, r.data)
        results = r.data["results"] if isinstance(r.data, dict) and "results" in r.data else r.data
        ids = {str(row["id"]) for row in results}
        self.assertIn(str(self.receipt_keeper.id), ids)
        self.assertIn(str(self.receipt_keeper2.id), ids)

    # ---- order isolation: customer sees only own -----------------------
    def test_customer_sees_only_own_orders(self):
        self._clear_auth()
        self._auth(f"Bearer {mint_customer_token(self.client_a)}")
        r = self.client.get("/api/customer/orders/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        ids = {str(row["id"]) for row in r.data}
        self.assertIn(str(self.receipt_keeper.id), ids)        # client_a's receipt
        self.assertNotIn(str(self.receipt_keeper2.id), ids)    # client_b's receipt

    # ---- customer login by phone ---------------------------------------
    def test_customer_login_nonexistent_phone(self):
        self._clear_auth()
        r = self.client.post(
            "/api/customer/login/", {"phone": "+996555999999"}, format="json",
        )
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["detail"], "Клиент с таким номером не найден")

    def test_customer_login_empty_phone(self):
        self._clear_auth()
        r = self.client.post("/api/customer/login/", {"phone": ""}, format="json")
        self.assertEqual(r.status_code, 400, getattr(r, "data", None))
        self.assertEqual(r.data["detail"], "Введите номер телефона")

    def test_customer_login_normalizes_phone_format(self):
        # Same digits as client_a's +996700000001, different punctuation.
        self._clear_auth()
        r = self.client.post(
            "/api/customer/login/",
            {"phone": "+996 (700) 00-00-01"}, format="json",
        )
        self.assertEqual(r.status_code, 200, getattr(r, "data", None))
        self.assertIn("access", r.data)
        self.assertEqual(r.data["client"]["id"], self.client_a.id)
