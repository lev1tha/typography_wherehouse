"""Правки по ревизии: удаление карточки, клиент без ФИО, кольцевой реферал."""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt


class DeleteClientTests(APITestCase):
    """У клиента есть заказы — отказ должен быть понятным, а не пятисоткой.

    `Receipt.client` стоит на PROTECT, и это правильно, но `ProtectedError`
    нигде не ловился: защита данных выглядела как поломка системы.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="dc_admin", password="x", role=User.Role.ADMIN
        )
        self.with_orders = Client.objects.create(full_name="Тахир", phone="+996555111000")
        self.clean = Client.objects.create(full_name="Пустой", phone="+996555111001")
        Receipt.objects.create(
            client=self.with_orders, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            total_price=Decimal("100"),
        )
        self.client.force_authenticate(self.admin)

    def test_client_with_orders_is_refused_politely(self):
        r = self.client.delete(f"/api/clients/clients/{self.with_orders.id}/")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("объедините", r.data["detail"].lower())
        self.assertTrue(Client.objects.filter(pk=self.with_orders.pk).exists())

    def test_client_without_orders_is_deleted(self):
        r = self.client.delete(f"/api/clients/clients/{self.clean.id}/")
        self.assertEqual(r.status_code, 204, getattr(r, "data", None))
        self.assertFalse(Client.objects.filter(pk=self.clean.pk).exists())


class ClientRequiredFieldsTests(APITestCase):
    """Карточка без ФИО не должна заводиться.

    При создании без явного `type` проверка промахивалась мимо: тип выходил
    None, обе ветки не срабатывали, а модель ставила PHYSICAL по умолчанию.
    Дальше карточка запиралась — любая правка упиралась в «укажите ФИО».
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cr_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)

    def test_phone_only_client_is_refused(self):
        r = self.client.post(
            "/api/clients/clients/", {"phone": "+996700999001"}, format="json"
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("full_name", r.data)

    def test_named_client_is_created(self):
        r = self.client.post(
            "/api/clients/clients/",
            {"phone": "+996700999002", "full_name": "Айбек"}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)

    def test_created_client_stays_editable(self):
        """Правка одного ИНН не должна требовать переслать ФИО."""
        made = self.client.post(
            "/api/clients/clients/",
            {"phone": "+996700999003", "full_name": "Бакыт"}, format="json",
        ).data
        r = self.client.patch(
            f"/api/clients/clients/{made['id']}/", {"inn": "12345"}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)


class ReferralCycleTests(APITestCase):
    """«А привёл Б, Б привёл А» — кольцо, по которому бонусы считаются в обе
    стороны. Проверка была только на «сам себя»."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="rc_admin", password="x", role=User.Role.ADMIN
        )
        self.a = Client.objects.create(full_name="А", phone="+996700888001")
        self.b = Client.objects.create(full_name="Б", phone="+996700888002")
        self.c = Client.objects.create(full_name="В", phone="+996700888003")
        self.client.force_authenticate(self.admin)

    def _set_referrer(self, client_obj, referrer):
        return self.client.patch(
            f"/api/clients/clients/{client_obj.id}/",
            {"referred_by": referrer.id}, format="json",
        )

    def test_direct_cycle_is_refused(self):
        self.assertEqual(self._set_referrer(self.a, self.b).status_code, 200)
        r = self._set_referrer(self.b, self.a)
        self.assertEqual(r.status_code, 400, r.data)
        self.b.refresh_from_db()
        self.assertIsNone(self.b.referred_by_id)

    def test_long_cycle_is_refused(self):
        """А ← Б ← В, и попытка замкнуть В ← А."""
        self.assertEqual(self._set_referrer(self.a, self.b).status_code, 200)
        self.assertEqual(self._set_referrer(self.b, self.c).status_code, 200)
        r = self._set_referrer(self.c, self.a)
        self.assertEqual(r.status_code, 400, r.data)

    def test_self_referral_still_refused(self):
        r = self._set_referrer(self.a, self.a)
        self.assertEqual(r.status_code, 400, r.data)

    def test_normal_chain_is_allowed(self):
        self.assertEqual(self._set_referrer(self.a, self.b).status_code, 200)
        self.assertEqual(self._set_referrer(self.b, self.c).status_code, 200)
