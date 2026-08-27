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


class CustomerOrderIsolationTests(APITestCase):
    """Кабинет отдаёт ТОЛЬКО свои заказы.

    Изоляция держится на одной строке во вьюхе (`filter(client=...)`), а цена
    ошибки здесь выше, чем везде: клиенты в Бишкеке друг друга знают, и чужой
    заказ в списке — это не «неудобство интерфейса», а утечка. Строку без теста
    может снять любая последующая правка — например, «показывать заказы всей
    организации», — и никто этого не заметит.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="iso_admin", password="x", role=User.Role.ADMIN
        )
        self.material = Material.objects.create(
            name="Плёнка", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("50"), purchase_price=Decimal("30"),
        )
        self.mine = self._customer("Айбек", "+996700777111", "mine99")
        self.stranger = self._customer("Чужой", "+996700777222", "hers99")

    def _customer(self, name, phone, password):
        c = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name=name, phone=phone
        )
        c.set_password(password)
        c.save()
        return c

    def _order(self, customer, quantity):
        """Заказ на клиента; количество разное, чтобы отличать заказы в выдаче."""
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "client_id": customer.id,
            "items": [{
                "type": "MATERIAL", "material": self.material.id,
                "quantity": quantity,
            }],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.client.force_authenticate(None)
        return r.data["id"]

    def _token(self, phone, password):
        r = self.client.post(
            LOGIN, {"phone": phone, "password": password}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["access"]

    def _orders_of(self, phone, password):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self._token(phone, password)}"
        )
        r = self.client.get("/api/customer/orders/")
        self.client.credentials()
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_customer_sees_only_own_orders(self):
        mine = self._order(self.mine, 1)
        hers = self._order(self.stranger, 2)

        ids = {o["id"] for o in self._orders_of("+996700777111", "mine99")}
        self.assertIn(mine, ids)
        self.assertNotIn(hers, ids, "в кабинете виден ЧУЖОЙ заказ — утечка")

    def test_isolation_holds_in_both_directions(self):
        """Не «первый клиент видит всё, второй ничего», а каждый — своё."""
        mine = self._order(self.mine, 1)
        hers = self._order(self.stranger, 2)

        self.assertEqual(
            [o["id"] for o in self._orders_of("+996700777111", "mine99")], [mine]
        )
        self.assertEqual(
            [o["id"] for o in self._orders_of("+996700777222", "hers99")], [hers]
        )

    def test_customer_without_orders_sees_an_empty_list(self):
        """Пустой список, а не чужие заказы и не ошибка."""
        self._order(self.stranger, 2)
        self.assertEqual(self._orders_of("+996700777111", "mine99"), [])

    def test_orders_of_a_deleted_link_do_not_leak(self):
        """Заказ без клиента (розница «без имени») не принадлежит никому."""
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{
                "type": "MATERIAL", "material": self.material.id, "quantity": 1,
            }],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.client.force_authenticate(None)

        self.assertEqual(self._orders_of("+996700777111", "mine99"), [])

    def test_customer_token_does_not_open_the_staff_api(self):
        """Токен кабинета — не пропуск в чеки цеха с себестоимостью и маржой."""
        self._order(self.mine, 1)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self._token('+996700777111', 'mine99')}"
        )
        for url in (
            "/api/sales/receipts/",
            "/api/warehouse/materials/",
            "/api/clients/",
            "/api/finance/report/",
        ):
            r = self.client.get(url)
            self.assertIn(
                r.status_code, (401, 403),
                f"клиентский токен пустили в {url} — {r.status_code}",
            )
        self.client.credentials()
