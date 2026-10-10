"""Смена пароля отзывает токены; ротация refresh; токены без клейма живут.

Раньше украденный токен (access 12 ч, refresh 7 дней) работал после смены
пароля — отозвать его было нечем.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.models import User
from clients.customer import mint_customer_token
from clients.models import Client


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class StaffTokenRevocationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="rev_admin", password="old-pass-1", role=User.Role.ADMIN
        )

    def tearDown(self):
        cache.clear()

    def _login(self, password):
        return self.client.post(
            "/api/token/", {"username": "rev_admin", "password": password}, format="json"
        )

    def _change_password(self, new):
        u = User.objects.get(pk=self.user.pk)
        u.set_password(new)
        u.save()

    def test_token_carries_the_credentials_version(self):
        r = self._login("old-pass-1")
        self.assertEqual(AccessToken(r.data["access"])["cv"], 0)

    def test_password_change_invalidates_access_and_refresh(self):
        r = self._login("old-pass-1")
        access, refresh = r.data["access"], r.data["refresh"]
        self.assertEqual(self.client.get("/api/me/", **bearer(access)).status_code, 200)

        self._change_password("new-pass-2")

        me = self.client.get("/api/me/", **bearer(access))
        self.assertEqual(me.status_code, 401)
        again = self.client.post("/api/token/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(again.status_code, 401)
        # Новый вход работает и даёт токен новой версии.
        fresh = self._login("new-pass-2")
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(
            self.client.get("/api/me/", **bearer(fresh.data["access"])).status_code, 200
        )

    def test_token_without_claim_stays_valid_until_password_changes(self):
        """Токен, выданный до введения версий, не должен вылетать при выкладке."""
        legacy = RefreshToken.for_user(self.user)  # без клейма cv
        self.assertNotIn("cv", legacy.payload)
        legacy_access = str(legacy.access_token)
        self.assertEqual(self.client.get("/api/me/", **bearer(legacy_access)).status_code, 200)
        r = self.client.post("/api/token/refresh/", {"refresh": str(legacy)}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

        self._change_password("new-pass-2")
        self.assertEqual(self.client.get("/api/me/", **bearer(legacy_access)).status_code, 401)

    def test_rehash_on_login_does_not_revoke_other_sessions(self):
        """Django сам обновляет хеш при входе (update_fields=['password']) —
        пароль тот же, токены других устройств живут."""
        first = self._login("old-pass-1")
        u = User.objects.get(pk=self.user.pk)
        u.set_password("old-pass-1")          # тот же пароль, новая соль
        u.save(update_fields=["password"])
        me = self.client.get("/api/me/", **bearer(first.data["access"]))
        self.assertEqual(me.status_code, 200)

    def test_refresh_rotates_and_blacklists_the_used_token(self):
        r = self._login("old-pass-1")
        old = r.data["refresh"]
        new = self.client.post("/api/token/refresh/", {"refresh": old}, format="json")
        self.assertEqual(new.status_code, 200, new.data)
        self.assertIn("access", new.data)
        self.assertIn("refresh", new.data)
        self.assertNotEqual(new.data["refresh"], old)
        # Использованный refresh больше не работает, новый — работает.
        replay = self.client.post("/api/token/refresh/", {"refresh": old}, format="json")
        self.assertEqual(replay.status_code, 401)
        ok = self.client.post(
            "/api/token/refresh/", {"refresh": new.data["refresh"]}, format="json"
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(AccessToken(ok.data["access"])["cv"], 0)

    def test_refresh_for_deleted_user_is_401_not_500(self):
        r = self._login("old-pass-1")
        self.user.delete()
        again = self.client.post("/api/token/refresh/", {"refresh": r.data["refresh"]}, format="json")
        self.assertEqual(again.status_code, 401)


class CustomerTokenRevocationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555777888")
        self.customer.set_password("portal-1")
        self.customer.save()
        self.admin = User.objects.create_user(
            username="rev_boss", password="x-boss-1", role=User.Role.ADMIN
        )

    def tearDown(self):
        cache.clear()

    def _orders(self, token):
        return self.client.get("/api/customer/orders/", **bearer(token))

    def test_new_password_from_admin_invalidates_the_old_token(self):
        login = self.client.post(
            "/api/customer/login/",
            {"phone": "+996555777888", "password": "portal-1"},
            format="json",
        )
        token = login.data["access"]
        self.assertEqual(self._orders(token).status_code, 200)

        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/clients/clients/{self.customer.pk}/set-password/", {"password": "portal-2"})
        self.assertEqual(r.status_code, 200, r.data)
        self.client.force_authenticate(None)

        self.assertEqual(self._orders(token).status_code, 401)
        relogin = self.client.post(
            "/api/customer/login/",
            {"phone": "+996555777888", "password": "portal-2"},
            format="json",
        )
        self.assertEqual(self._orders(relogin.data["access"]).status_code, 200)

    def test_token_without_claim_is_version_zero(self):
        """Токен кабинета, выданный до введения версий, работает до смены пароля."""
        fresh = Client.objects.create(full_name="Новый", phone="+996700000001")
        token = AccessToken()
        token["scope"] = "customer"
        token["client_id"] = fresh.id           # без cv
        self.assertEqual(self._orders(str(token)).status_code, 200)

    def test_minted_token_carries_version(self):
        self.assertEqual(AccessToken(mint_customer_token(self.customer))["cv"], 1)
