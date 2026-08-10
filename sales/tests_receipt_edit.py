"""Ошибочный чек: найти, поправить безопасное, удалить целиком.

До этого `PATCH` и `DELETE` на чеке были открыты ЛЮБОМУ авторизованному и без
единой проверки: складовщик мог переписать `total_price` любым числом, а DELETE
стирал чек, не возвращая материал на склад. Кнопок в интерфейсе не было, поэтому
дыру никто не замечал — «изменить чек нельзя» и «чек защищён» разные вещи.

Правило теперь такое:
  - править можно наименование, клиента и дату заказа — то, что не двигает
    деньги и склад;
  - ошибочный СОСТАВ исправляется удалением и повторным вводом;
  - удаление возвращает материал на склад и стирает движения по чеку, но
    оставляет запись в журнале действий;
  - и то и другое — только админ.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from clients.models import Client
from sales.models import Receipt
from sales.sale_service import create_sale
from services.models import PrintingService
from warehouse.models import InventoryLog, Material


class ReceiptEditDeleteTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss_ed", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="skl_ed", password="x", role=User.Role.STOREKEEPER
        )
        self.client_obj = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.other = Client.objects.create(full_name="Нурлан", phone="+996555333444")
        self.material = Material.objects.create(
            name="Акрил",
            unit=Material.Unit.SQM,
            quantity=Decimal("100"),
            price_per_sqm=Decimal("1000"),
            purchase_price=Decimal("600"),
            cut_rate_per_pm=Decimal("50"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC,
        )

    def _sale(self, *, client=None, width="2", length="3"):
        return create_sale(
            client=client or self.client_obj,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "SERVICE", "service": self.cutting, "material": self.material,
                "width": width, "length": length, "running_meters": "10",
            }],
            amount_paid=None,
        )

    def _url(self, receipt):
        return f"/api/sales/receipts/{receipt.id}/"

    # ---- правка безопасных полей ------------------------------------------
    def test_admin_edits_title_client_and_order_date(self):
        r = self._sale()
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(
            self._url(r),
            {"title": "Вывеска для кафе", "client": self.other.id, "order_date": "2026-07-15"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        r.refresh_from_db()
        self.assertEqual(r.title, "Вывеска для кафе")
        self.assertEqual(r.client_id, self.other.id)
        self.assertEqual(timezone.localtime(r.created_at).date().isoformat(), "2026-07-15")

    def test_order_date_moves_stock_write_off_with_it(self):
        """Дата заказа опорная для ВСЕЙ отчётности. Расход, оставшийся в прежнем
        месяце, увёл бы складской лист от выручки."""
        r = self._sale()
        self.client.force_authenticate(self.admin)
        self.client.patch(self._url(r), {"order_date": "2026-07-15"}, format="json")
        log = r.inventory_logs.filter(type=InventoryLog.Type.SALE).first()
        self.assertEqual(timezone.localtime(log.happened_at).date().isoformat(), "2026-07-15")

    def test_money_fields_are_rejected_with_an_explanation(self):
        """Раньше это проходило: `total_price` переписывался любым числом."""
        r = self._sale()
        before = r.total_price
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(self._url(r), {"total_price": "999999"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("удалить чек и завести заново", resp.data["detail"])
        r.refresh_from_db()
        self.assertEqual(r.total_price, before)

    def test_future_order_date_is_rejected(self):
        r = self._sale()
        self.client.force_authenticate(self.admin)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        resp = self.client.patch(self._url(r), {"order_date": tomorrow}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_storekeeper_cannot_edit(self):
        r = self._sale()
        self.client.force_authenticate(self.keeper)
        resp = self.client.patch(self._url(r), {"title": "моё"}, format="json")
        self.assertEqual(resp.status_code, 403)

    # ---- удаление ----------------------------------------------------------
    def test_delete_returns_material_to_stock(self):
        before = self.material.quantity
        r = self._sale()
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, before - Decimal("6"))  # 2 × 3 кв.м

        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(self._url(r)).status_code, 204)
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, before)
        self.assertFalse(Receipt.objects.filter(pk=r.pk).exists())

    def test_delete_clears_stock_journal_for_that_receipt(self):
        """Удаление — не возврат: показывать «продажа по чеку №N / возврат по
        чеку №N» для заказа, которого нет, значит врать журналу, а «проданные» в
        складском листе считались бы по несуществующей продаже."""
        r = self._sale()
        self.assertTrue(InventoryLog.objects.filter(receipt=r).exists())
        self.client.force_authenticate(self.admin)
        self.client.delete(self._url(r))
        self.assertFalse(
            InventoryLog.objects.filter(reason__contains=f"№{r.order_number}").exists()
        )

    def test_delete_removes_payments(self):
        r = self._sale()
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{r.id}/pay/", {"amount": "500"})
        self.assertEqual(r.payments.count(), 1)
        self.client.delete(self._url(r))
        self.assertFalse(Receipt.objects.filter(pk=r.pk).exists())

    def test_delete_leaves_a_trace_in_the_action_log(self):
        """От чека не остаётся ничего — значит на вопрос «что там было» отвечает
        журнал действий, и состав в нём должен быть."""
        r = self._sale()
        self.client.force_authenticate(self.admin)
        self.client.delete(self._url(r))
        entry = AuditLog.objects.order_by("-id").first()
        self.assertIn("Удалён чек", entry.action)
        self.assertIn(f"№{r.order_number}", entry.action)
        self.assertIn("Акрил", entry.action)

    def test_storekeeper_cannot_delete(self):
        r = self._sale()
        self.client.force_authenticate(self.keeper)
        self.assertEqual(self.client.delete(self._url(r)).status_code, 403)
        self.assertTrue(Receipt.objects.filter(pk=r.pk).exists())

    def test_returned_lines_are_not_restored_twice(self):
        """По возвращённой строке материал уже вернулся при возврате. Положить
        его на склад второй раз значило бы нарисовать материал из воздуха."""
        before = self.material.quantity
        r = self._sale()
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{r.id}/refund/", {}, format="json")
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, before)

        self.client.delete(self._url(r))
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, before)


class ReceiptFilterTests(APITestCase):
    """Фильтры списка чеков: по клиенту и по дате ЗАКАЗА.

    «Найти все заказы Тахира за июль» через поиск по строке не спрашивается:
    поиск ищет одно слово, а не пересечение двух условий.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss_f", password="x", role=User.Role.ADMIN
        )
        self.a = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.b = Client.objects.create(full_name="Нурлан", phone="+996555333444")
        self.july = self._receipt(self.a, "2026-07-15")
        self.august = self._receipt(self.a, "2026-08-03")
        self.other = self._receipt(self.b, "2026-07-20")

    def _receipt(self, client, day):
        from sales.sale_service import day_to_moment
        from datetime import date

        r = Receipt.objects.create(
            client=client,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            payment_status=Receipt.PaymentStatus.PAID,
            total_price=Decimal("100"),
            amount_paid=Decimal("100"),
        )
        Receipt.objects.filter(pk=r.pk).update(created_at=day_to_moment(date.fromisoformat(day)))
        r.refresh_from_db()
        return r

    def _numbers(self, params):
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/sales/receipts/", params)
        self.assertEqual(resp.status_code, 200, resp.data)
        return {row["order_number"] for row in resp.data["results"]}

    def test_filter_by_client(self):
        got = self._numbers({"client": self.a.id})
        self.assertEqual(got, {self.july.order_number, self.august.order_number})

    def test_filter_by_date_range(self):
        got = self._numbers({"date_from": "2026-07-01", "date_to": "2026-07-31"})
        self.assertEqual(got, {self.july.order_number, self.other.order_number})

    def test_client_and_period_together(self):
        got = self._numbers(
            {"client": self.a.id, "date_from": "2026-07-01", "date_to": "2026-07-31"}
        )
        self.assertEqual(got, {self.july.order_number})

    def test_broken_date_is_ignored_not_a_500(self):
        """Опечатка в адресе не должна ронять список."""
        got = self._numbers({"date_from": "вчера"})
        self.assertEqual(len(got), 3)

    def test_stats_follow_the_same_filters(self):
        """Иначе «Долг» показывал бы общий долг цеха под списком одного клиента."""
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/sales/receipts/stats/", {"client": self.a.id})
        self.assertEqual(resp.data["total"], 2)
