"""Стоимость склада — на конец ВЫБРАННОГО периода, а не всегда сегодняшняя.

Отчёт за месяц показывал сегодняшний склад, какой бы месяц ни открыли: в
августе, где не было ни одной продажи и ни одного прихода, выручка 0, закуп 0,
а склад 1 184 614. Цифра из другого времени стояла среди месячных и читалась
как месячная.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from sales.sale_service import create_sale
from warehouse.models import InventoryLog, Material, Roll, stock_value_total
from warehouse.rolls import receive_lot

# «Сегодня» для этих тестов — середина месяца. Тесты считают «позавчера»,
# «вчера» и «прошлый месяц» от сегодняшнего дня; запущенные 1-го и 2-го числа
# они клали партию в ПРОШЛЫЙ месяц и ждали там пустой склад — и падали раз в
# месяц. Фиксируем часы, а не пропускаем тесты.
FROZEN_NOW = timezone.make_aware(datetime(2026, 10, 15, 12, 0))


def freeze_now(testcase, moment=FROZEN_NOW):
    """Остановить «сейчас» на время теста — ВКЛЮЧАЯ значения по умолчанию полей.

    `timezone.now` подменяется как функция модуля, но поля вроде
    `happened_at = DateTimeField(default=timezone.now)` держат ссылку на
    ОРИГИНАЛ, взятую при импорте, и подмена модуля их не касается — запись
    журнала получила бы настоящее время. Поэтому подменяем и их значение по
    умолчанию.
    """
    patchers = [mock.patch("django.utils.timezone.now", return_value=moment)]
    for model, name in [(InventoryLog, "happened_at"), (Roll, "received_at"),
                        (Receipt, "created_at")]:
        field = model._meta.get_field(name)
        patchers.append(mock.patch.object(field, "_get_default", lambda: moment))
    for patcher in patchers:
        patcher.start()
        testcase.addCleanup(patcher.stop)


class StockValueOnDateTests(APITestCase):
    REPORT = "/api/finance/report/"

    def setUp(self):
        freeze_now(self)
        self.admin = User.objects.create_user(
            username="sd_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.today = timezone.localdate()
        self.material = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"),
        )
        # Партия 10 кв.м за 2000 пришла ПОЗАВЧЕРА.
        self.lot = receive_lot(
            self.material, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("10"),
            purchase_cost=Decimal("2000"),
            received_at=timezone.now() - timedelta(days=2),
        )

    def test_before_the_first_intake_the_warehouse_was_empty(self):
        """Журнал начинается приходом — до него склада не было вовсе."""
        self.assertEqual(stock_value_total(self.today - timedelta(days=30)), Decimal("0.00"))
        self.assertEqual(stock_value_total(self.today - timedelta(days=3)), Decimal("0.00"))

    def test_on_the_day_of_the_intake_it_is_the_whole_lot(self):
        self.assertEqual(stock_value_total(self.today - timedelta(days=2)), Decimal("2000.00"))

    def test_a_sale_lowers_it_from_its_own_day(self):
        create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{"type": "MATERIAL", "material": self.material,
                         "quantity": Decimal("2"), "mode": "SQM"}],
            amount_paid=Decimal("0"),
        )
        # Вчера продажи ещё не было.
        self.assertEqual(stock_value_total(self.today - timedelta(days=1)), Decimal("2000.00"))
        # Сегодня ушло 2 кв.м по 200 — осталось 1600.
        self.assertEqual(stock_value_total(self.today), Decimal("1600.00"))

    def test_today_and_later_use_the_exact_current_formula(self):
        """«Сейчас» считается по остаткам партий, а не реконструкцией."""
        exact = stock_value_total()
        self.assertEqual(stock_value_total(self.today), exact)
        self.assertEqual(stock_value_total(self.today + timedelta(days=5)), exact)

    def test_report_for_a_past_month_shows_an_empty_warehouse(self):
        """Тот самый случай: месяц без движений — склад ноль, а не сегодняшний."""
        first = self.today.replace(day=1)
        prev_end = first - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        data = self.client.get(self.REPORT, {
            "date_from": prev_start.isoformat(), "date_to": prev_end.isoformat(),
        }).data
        self.assertEqual(Decimal(str(data["revenue"])), Decimal("0"))
        self.assertEqual(Decimal(str(data["stock"]["value_now"])), Decimal("0.00"))
        self.assertEqual(data["stock"]["as_of"], prev_end.isoformat())

    def test_current_month_keeps_todays_number_and_no_date_label(self):
        first = self.today.replace(day=1)
        data = self.client.get(self.REPORT, {
            "date_from": first.isoformat(),
            "date_to": (self.today + timedelta(days=20)).isoformat(),
        }).data
        self.assertEqual(Decimal(str(data["stock"]["value_now"])), stock_value_total())
        self.assertIsNone(data["stock"]["as_of"])

    def test_reconciliation_chain_ends_on_the_period_stock(self):
        """Цепочка «было → пришло → продали → списали → лежит» идёт по периоду.

        Итог цепочки — остаток на конец ПЕРИОДА, иначе строки складывались бы
        за месяц, а итог был бы за сегодня.
        """
        prev_end = self.today.replace(day=1) - timedelta(days=1)
        data = self.client.get(self.REPORT, {
            "date_from": prev_end.replace(day=1).isoformat(), "date_to": prev_end.isoformat(),
        }).data
        rec = data["stock"]["reconcile"]
        self.assertEqual(Decimal(str(rec["value_now"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(rec["opening"])), Decimal("0.00"))
        self.assertEqual(Decimal(str(rec["gap"])), Decimal("0.00"))

    def test_chain_adds_up_for_the_current_month(self):
        """было + закуп − продано − списано = лежит на конец."""
        first = self.today.replace(day=1)
        rec = self.client.get(self.REPORT, {
            "date_from": first.isoformat(), "date_to": self.today.isoformat(),
        }).data["stock"]["reconcile"]
        chain = (Decimal(str(rec["opening"])) + Decimal(str(rec["purchases"]))
                 - Decimal(str(rec["cogs"])) - Decimal(str(rec["losses"])))
        self.assertEqual(chain, Decimal(str(rec["expected"])))
        self.assertEqual(
            Decimal(str(rec["expected"])) - Decimal(str(rec["value_now"])),
            Decimal(str(rec["gap"])),
        )

    def test_overview_follows_the_same_period(self):
        prev_end = self.today.replace(day=1) - timedelta(days=1)
        data = self.client.get("/api/audit/dashboard/", {
            "date_from": prev_end.replace(day=1).isoformat(), "date_to": prev_end.isoformat(),
        }).data
        self.assertEqual(Decimal(str(data["unrealised_asset"])), Decimal("0.00"))
