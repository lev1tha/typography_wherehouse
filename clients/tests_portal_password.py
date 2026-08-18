"""Клиентский портал: вход по телефону + паролю, который выдаёт АДМИН.

Пароль клиент себе не заводит: иначе кабинет доставался бы тому, кто первым
вошёл по чужому номеру. Складовщик пароль выдавать не может — только админ.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from services.models import PrintingService
from warehouse.models import Material

LOGIN = "/api/customer/login/"


class CustomerLoginTests(APITestCase):
    def setUp(self):
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Айбек", phone="+996700111222"
        )
        self.other = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Чужой", phone="+996700333444"
        )

    def _login(self, phone, password=None):
        body = {"phone": phone}
        if password is not None:
            body["password"] = password
        return self.client.post(LOGIN, body, format="json")

    # ---- пока пароль не выдан -----------------------------------------------
    def test_without_issued_password_login_is_impossible(self):
        r = self._login("+996700111222")
        self.assertEqual(r.status_code, 200, r.data)
        # Ответ на шаг «только телефон» одинаковый для всех: портал открыт на
        # публичном домене, и по нему нельзя выяснять, кто у цеха заказывает.
        self.assertEqual(r.data["status"], "need_password")
        self.assertNotIn("access", r.data)
        self.assertNotIn("name", r.data)

    def test_client_cannot_set_own_password(self):
        """Главное: подобранный «пароль» больше не становится постоянным —
        раньше первый вошедший по чужому номеру захватывал кабинет.

        Отказ теперь такой же, как при неверном пароле у клиента с паролем:
        по ответу не видно, выдан пароль этому номеру или нет.
        """
        r = self._login("+996700111222", "ЯПридумалСам")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["detail"], "Неверный номер или пароль.")
        self.assertNotIn("access", r.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.has_password)

    # ---- обычный вход --------------------------------------------------------
    def test_asks_for_password_when_issued(self):
        self.customer.set_password("issued99")
        self.customer.save()
        r = self._login("+996700111222")
        self.assertEqual(r.data["status"], "need_password")
        # Имя — только после пароля.
        self.assertNotIn("name", r.data)

    def test_first_step_is_identical_for_a_stranger_and_a_customer(self):
        """Портал не подтверждает, что номер есть в базе.

        Раньше известный номер получал «С возвращением, Бакыт Осмонов!», а
        неизвестный — «Клиент с таким номером не найден»: перебором номеров с
        публичной страницы собиралась клиентская база цеха с именами.
        """
        self.customer.set_password("issued99")
        self.customer.save()
        ours = self._login("+996700111222")
        stranger = self._login("+996700000000")
        self.assertEqual(ours.status_code, stranger.status_code)
        self.assertEqual(dict(ours.data), dict(stranger.data))

    def test_login_with_correct_password(self):
        self.customer.set_password("issued99")
        self.customer.save()
        r = self._login("+996700111222", "issued99")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)
        self.assertEqual(r.data["client"]["id"], self.customer.id)

    def test_login_with_wrong_password_rejected(self):
        self.customer.set_password("issued99")
        self.customer.save()
        r = self._login("+996700111222", "неверный")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertNotIn("access", r.data)

    def test_unknown_phone_rejected(self):
        # С паролем: отказ такой же, как при неверном пароле своего номера —
        # по тексту нельзя понять, existed ли номер вообще.
        r = self._login("+996700000000", "любой")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data["detail"], "Неверный номер или пароль.")

    def test_empty_phone_rejected(self):
        r = self._login("")
        self.assertEqual(r.status_code, 400, r.data)

    def test_phone_format_is_normalised(self):
        self.customer.set_password("issued99")
        self.customer.save()
        r = self._login("+996 (700) 11-12-22", "issued99")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)


class IssuePasswordTests(APITestCase):
    """POST /clients/<id>/set-password/ — выдать пароль. Только админ."""

    def setUp(self):
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Нурбек", phone="+996700555666"
        )
        self.admin = User.objects.create_user(username="p_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="p_store", password="x", role=User.Role.STOREKEEPER)

    def _url(self):
        return f"/api/clients/clients/{self.customer.id}/set-password/"

    def _issue(self, **body):
        return self.client.post(self._url(), body, format="json")

    def test_storekeeper_cannot_issue(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self._issue().status_code, 403)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.has_password)

    def test_anonymous_cannot_issue(self):
        self.assertIn(self._issue().status_code, (401, 403))

    def test_admin_issues_generated_password(self):
        self.client.force_authenticate(self.admin)
        r = self._issue()
        self.assertEqual(r.status_code, 200, r.data)
        password = r.data["password"]
        self.assertEqual(len(password), 6)
        self.assertTrue(password.isdigit())
        self.customer.refresh_from_db()
        # В базе только хеш; выданным паролем клиент входит.
        self.assertNotEqual(self.customer.portal_password, password)
        self.assertTrue(self.customer.check_password(password))

    def test_admin_can_set_custom_password(self):
        self.client.force_authenticate(self.admin)
        r = self._issue(password="моипароль")
        self.assertEqual(r.status_code, 200, r.data)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.check_password("моипароль"))

    def test_too_short_custom_password_rejected(self):
        self.client.force_authenticate(self.admin)
        r = self._issue(password="ab")
        self.assertEqual(r.status_code, 400, r.data)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.has_password)

    def test_reissue_invalidates_the_old_one(self):
        self.client.force_authenticate(self.admin)
        first = self._issue().data["password"]
        second = self._issue().data["password"]
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.check_password(first))
        self.assertTrue(self.customer.check_password(second))

    def test_issued_password_actually_works_for_login(self):
        self.client.force_authenticate(self.admin)
        password = self._issue().data["password"]
        self.client.force_authenticate(None)
        r = self.client.post(LOGIN, {"phone": "+996700555666", "password": password}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)

    def test_serializer_exposes_has_password(self):
        self.client.force_authenticate(self.admin)
        r = self.client.get(f"/api/clients/clients/{self.customer.id}/")
        self.assertFalse(r.data["has_password"])
        self._issue()
        r = self.client.get(f"/api/clients/clients/{self.customer.id}/")
        self.assertTrue(r.data["has_password"])


class CustomerPortalPayloadTests(APITestCase):
    """Кабинет не показывает готовность там, где нечего готовить."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cp_admin", password="x", role=User.Role.ADMIN
        )
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Айбек", phone="+996700555111"
        )
        self.customer.set_password("pass99")
        self.customer.save()
        self.material = Material.objects.create(
            name="Бумага", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("50"), purchase_price=Decimal("30"),
        )
        self.service = PrintingService.objects.create(
            name="Установка", kind=PrintingService.Kind.OTHER, base_price=Decimal("300")
        )

    def _order(self, items):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "client_id": self.customer.id,
            "items": items,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.client.force_authenticate(None)

    def _my_orders(self):
        r = self.client.post(
            LOGIN, {"phone": "+996700555111", "password": "pass99"}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        orders = self.client.get("/api/customer/orders/")
        self.client.credentials()
        return orders.data

    def test_material_only_order_has_no_production_status(self):
        """Куплен лист бумаги — ждать нечего. Раньше кабинет писал «Готовится»,
        причём навсегда: у цеха на такой заказ кнопки готовности нет."""
        self._order([{"type": "MATERIAL", "material": self.material.id, "quantity": 1}])
        order = self._my_orders()[0]
        self.assertFalse(order["has_service"])

    def test_order_with_work_keeps_its_production_status(self):
        self._order([{"type": "SERVICE", "service": self.service.id, "quantity": 1}])
        order = self._my_orders()[0]
        self.assertTrue(order["has_service"])
        self.assertEqual(order["fulfillment_status"], "PROCESSING")
