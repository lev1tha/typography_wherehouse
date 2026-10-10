"""Телефон клиента: формат, смена админом, отзыв токенов кабинета.

Телефон — логин кабинета клиента. Раньше принимался «1» и «абв», а менять его
мог любой, кто вправе править карточку; смена оставляла старые токены в силе.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from clients.models import Client

URL = "/api/clients/clients/"


class PhoneFormatTests(APITestCase):
    def setUp(self):
        self.keeper = User.objects.create_user(username="ph_keeper", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.keeper)

    def post(self, phone):
        return self.client.post(URL, {"type": "PHYSICAL", "full_name": "Тест", "phone": phone}, format="json")

    def test_garbage_is_refused_with_a_clear_message(self):
        for phone in ("1", "абв", "12345678", "+996 55", "1" * 16):
            with self.subTest(phone=phone):
                r = self.post(phone)
                self.assertEqual(r.status_code, 400, r.data)
                self.assertIn("от 9 до 15 цифр", str(r.data["phone"]))
        self.assertEqual(Client.objects.count(), 0)

    def test_usual_formats_still_pass(self):
        for i, phone in enumerate(("+996555112233", "0555 11 22 34", "(555) 11-22-35", "996 555 112 236")):
            with self.subTest(phone=phone):
                self.assertEqual(self.post(phone).status_code, 201)

    def test_legacy_short_phone_does_not_block_other_edits(self):
        legacy = Client.objects.create(full_name="Старый", phone="12345")
        r = self.client.patch(f"{URL}{legacy.pk}/", {"full_name": "Новый", "phone": "12345"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)


class PhoneChangeTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(username="ph_admin", password="x", role=User.Role.ADMIN)
        self.keeper = User.objects.create_user(username="ph_keeper2", password="x", role=User.Role.STOREKEEPER)
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.customer.set_password("portal-1")
        self.customer.save()
        self.other = Client.objects.create(full_name="Другой", phone="+996700333444")
        self.url = f"{URL}{self.customer.pk}/"

    def tearDown(self):
        cache.clear()

    def _portal_token(self, phone="+996555111222"):
        r = self.client.post("/api/customer/login/", {"phone": phone, "password": "portal-1"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["access"]

    def _orders(self, token):
        return self.client.get("/api/customer/orders/", HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_admin_changes_phone_and_old_token_dies(self):
        token = self._portal_token()
        self.assertEqual(self._orders(token).status_code, 200)

        self.client.force_authenticate(self.admin)
        r = self.client.patch(self.url, {"phone": "+996555999888"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.client.force_authenticate(None)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone, "+996555999888")
        self.assertEqual(self._orders(token).status_code, 401)
        # Новый логин работает, старый номер — нет.
        self.assertEqual(self._orders(self._portal_token("+996555999888")).status_code, 200)
        old = self.client.post("/api/customer/login/",
                               {"phone": "+996555111222", "password": "portal-1"}, format="json")
        self.assertEqual(old.status_code, 400)

    def test_change_is_written_to_the_audit_log(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(self.url, {"phone": "+996555999888"}, format="json")
        entry = AuditLog.objects.filter(action__startswith="Изменён телефон клиента").get()
        self.assertEqual(entry.user, self.admin)
        self.assertIn("+996555111222", entry.action)
        self.assertIn("+996555999888", entry.action)

    def test_duplicate_by_normalised_key_is_refused(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(self.url, {"phone": "0700 33 34 44"}, format="json")   # номер «Другого»
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("уже записан", str(r.data["phone"]))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone, "+996555111222")

    def test_storekeeper_cannot_change_the_number(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.patch(self.url, {"phone": "+996555999888"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("только администратор", str(r.data["phone"]))

    def test_respelling_the_same_number_keeps_tokens(self):
        token = self._portal_token()
        self.client.force_authenticate(self.keeper)
        r = self.client.patch(self.url, {"phone": "0555 111 222"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.client.force_authenticate(None)
        self.assertEqual(self._orders(token).status_code, 200)
        self.assertFalse(AuditLog.objects.filter(action__startswith="Изменён телефон").exists())


class ReferralValidationStillCallableTests(APITestCase):
    """Касса вызывает проверку реферера напрямую (`sales.views`) — имя и
    сигнатура обязаны остаться."""

    def test_validate_referred_by_is_callable_and_catches_a_loop(self):
        from rest_framework import serializers

        from clients.serializers import ClientSerializer

        a = Client.objects.create(full_name="А", phone="+996555000001")
        b = Client.objects.create(full_name="Б", phone="+996555000002", referred_by=a)
        with self.assertRaises(serializers.ValidationError):
            ClientSerializer(a, context={}).validate_referred_by(b)
