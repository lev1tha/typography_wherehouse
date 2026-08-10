"""Заказ задним числом: дата заказа задаётся вручную и тянет за собой отчёты.

`Receipt.created_at` перестал быть `auto_now_add` — заказчик заносит работы
позже, чем их делает. Дата опорная для всей отчётности, поэтому проверяем не
только «записалась», но и что по ней считают финансы, склад и карточка клиента.

Покрывает:
  - заказ создаётся указанной датой; без даты — «сейчас» (прежнее поведение)
  - будущая дата отклоняется
  - задним числом может только админ, складовщику — 403
  - списание материала в журнале датируется заказом, а не сегодняшним днём
  - месячный отчёт, отчёт по дням и складской лист относят заказ к его месяцу
  - карточка клиента показывает заказ в его периоде
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from warehouse.models import InventoryLog, Material


class BackdatedOrderTests(APITestCase):
    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_back", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="keeper_back", password="x", role=User.Role.STOREKEEPER
        )
        self.material = Material.objects.create(
            name="Форекс задним числом",
            unit=Material.Unit.SQM,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0"),
            purchase_price=Decimal("600"),
            piece_price=Decimal("1000"),
            piece_area=Decimal("1"),
        )
        self.client_one = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Заказчик", phone="+996700222"
        )

    @staticmethod
    def _month_bounds(day):
        """Границы месяца, в котором лежит `day` — так период задаёт интерфейс."""
        first = day.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return {"date_from": first.isoformat(), "date_to": last.isoformat()}

    def _checkout(self, *, user=None, order_date=None, amount_paid="1000"):
        self.client.force_authenticate(user or self.admin)
        body = {
            "payment_method": "CASH",
            "client_id": self.client_one.id,
            "amount_paid": amount_paid,
            "items": [
                {"type": "MATERIAL", "material": self.material.id, "quantity": 1, "mode": "PIECE"}
            ],
        }
        if order_date:
            body["order_date"] = order_date
        return self.client.post(self.URL, body, format="json")

    # ---- сама дата ---------------------------------------------------------

    def test_order_gets_the_given_date(self):
        past = timezone.localdate() - timedelta(days=20)
        resp = self._checkout(order_date=past.isoformat())
        self.assertEqual(resp.status_code, 201, resp.data)
        receipt = Receipt.objects.get(id=resp.data["id"])
        self.assertEqual(timezone.localtime(receipt.created_at).date(), past)

    def test_without_date_order_is_created_now(self):
        """Прежнее поведение не меняется: дата не передана — заказ сегодняшний."""
        resp = self._checkout()
        self.assertEqual(resp.status_code, 201, resp.data)
        receipt = Receipt.objects.get(id=resp.data["id"])
        self.assertEqual(timezone.localtime(receipt.created_at).date(), timezone.localdate())

    def test_future_date_is_rejected(self):
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        resp = self._checkout(order_date=future)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(Receipt.objects.exists())

    def test_backdating_is_admin_only(self):
        """Дата заказа двигает деньги закрытых месяцев — это право админа."""
        past = (timezone.localdate() - timedelta(days=5)).isoformat()
        resp = self._checkout(user=self.keeper, order_date=past)
        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertFalse(Receipt.objects.exists())

    def test_storekeeper_can_still_sell_today(self):
        """Сегодняшняя дата от складовщика проходит — это обычная продажа."""
        today = timezone.localdate().isoformat()
        resp = self._checkout(user=self.keeper, order_date=today)
        self.assertEqual(resp.status_code, 201, resp.data)

    # ---- склад -------------------------------------------------------------

    def test_stock_movement_is_dated_with_the_order(self):
        """Расход материала в журнале стоит датой заказа, а не сегодняшней.

        Иначе журнал показывал бы, что материал ушёл сегодня, по заказу за
        прошлый месяц — и «поступление минус продажи» в листе не сходилось бы.
        """
        past = timezone.localdate() - timedelta(days=15)
        resp = self._checkout(order_date=past.isoformat())
        self.assertEqual(resp.status_code, 201, resp.data)

        log = InventoryLog.objects.get(type=InventoryLog.Type.SALE, material=self.material)
        self.assertEqual(timezone.localtime(log.happened_at).date(), past)

    # ---- отчёты ------------------------------------------------------------

    def test_reports_count_the_order_in_its_own_month(self):
        """Заказ задним числом попадает в СВОЙ месяц во всех трёх отчётах."""
        # Берём заведомо прошлый месяц: первое число прошлого месяца.
        today = timezone.localdate()
        first_this = today.replace(day=1)
        target = (first_this - timedelta(days=1)).replace(day=1)
        resp = self._checkout(order_date=target.isoformat())
        self.assertEqual(resp.status_code, 201, resp.data)

        self.client.force_authenticate(self.admin)
        params = {"year": target.year, "month": target.month}

        # Месячный отчёт фильтруется границами периода (как его зовёт интерфейс),
        # а дневной и складской — годом с месяцем.
        report = self.client.get("/api/finance/report/", self._month_bounds(target))
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(Decimal(str(report.data["revenue"])), Decimal("1000"))
        # Себестоимость проданного тоже уехала в тот месяц (1 лист × 600).
        self.assertEqual(Decimal(str(report.data["cogs"])), Decimal("600"))

        daily = self.client.get("/api/finance/daily/", params)
        self.assertEqual(daily.status_code, 200, daily.data)
        day_row = next(r for r in daily.data["rows"] if r["date"] == target.isoformat())
        self.assertEqual(Decimal(str(day_row["revenue"])), Decimal("1000"))

        material = self.client.get("/api/finance/material-report/", params)
        self.assertEqual(material.status_code, 200, material.data)
        row = next(r for r in material.data["rows"] if r["id"] == self.material.id)
        self.assertEqual(Decimal(str(row["material_revenue"])), Decimal("1000"))
        self.assertEqual(row["orders"], 1)

    def test_current_month_report_does_not_see_the_backdated_order(self):
        """Обратная проверка: в текущем месяце этого заказа быть не должно."""
        today = timezone.localdate()
        target = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        self._checkout(order_date=target.isoformat())

        self.client.force_authenticate(self.admin)
        report = self.client.get("/api/finance/report/", self._month_bounds(today))
        self.assertEqual(report.status_code, 200, report.data)
        self.assertEqual(Decimal(str(report.data["revenue"])), Decimal("0"))

    def test_client_card_shows_the_order_in_its_period(self):
        past = timezone.localdate() - timedelta(days=10)
        self._checkout(order_date=past.isoformat())

        self.client.force_authenticate(self.admin)
        resp = self.client.get(
            f"/api/clients/clients/{self.client_one.id}/",
            {"date_from": past.isoformat(), "date_to": past.isoformat()},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(resp.data["orders"]), 1)

    def test_order_number_stays_sequential_not_chronological(self):
        """Номер заказа — регистрационный, он не переставляется под дату.

        Заказ задним числом получает СЛЕДУЮЩИЙ свободный номер: номера идут по
        порядку занесения, а не по датам работ.
        """
        first = self._checkout()  # сегодня
        past = (timezone.localdate() - timedelta(days=30)).isoformat()
        second = self._checkout(order_date=past)
        self.assertEqual(
            Receipt.objects.get(id=second.data["id"]).order_number,
            Receipt.objects.get(id=first.data["id"]).order_number + 1,
        )


class BackdatedOrderAdminGuardTests(APITestCase):
    """Вторая дверь к дате заказа — Django-админка.

    `created_at` перестал быть `auto_now_add`, то есть стал обычным
    редактируемым полем и сам по себе доступен в админке. Правило «задним
    числом — только админ» должно держаться и там, а не только в кассе.
    """

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="root_back", password="x", role=User.Role.ADMIN
        )
        self.staff = User.objects.create_user(
            username="staff_back", password="x", role=User.Role.STOREKEEPER, is_staff=True
        )

    def _readonly_for(self, user):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory

        from sales.admin import ReceiptAdmin

        request = RequestFactory().get("/django-admin/")
        request.user = user
        return ReceiptAdmin(Receipt, site).get_readonly_fields(request)

    def test_order_date_is_locked_for_non_superuser(self):
        self.assertIn("created_at", self._readonly_for(self.staff))

    def test_superuser_can_still_edit_the_date(self):
        self.assertNotIn("created_at", self._readonly_for(self.superuser))
