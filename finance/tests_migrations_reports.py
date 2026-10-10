"""Миграции этапа 1 (2026-10-07) — вперёд и назад на живых данных.

finance/0013–0015 и sales/0013–0014 обязаны: заполнить новые поля так, чтобы
прошлые месяцы не сдвинулись (роль по блоку, «за какой месяц» = месяц оплаты,
ставка налога только с октября 2026, неоплаченный онлайн-заказ не признан), и
откатываться без потерь — до состояния, в котором их не было.
"""
from datetime import date, datetime, time
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

BEFORE = [("finance", "0012_cash_financing_articles"), ("sales", "0012_returned_at_backfill")]
AFTER = [
    ("finance", "0015_cash_opening_and_lot_keeps_payment"),
    ("sales", "0014_revenue_recognized_backfill"),
]


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


class ReportsV2MigrationTests(TransactionTestCase):
    # Данные миграций (встроенные виды расхода) нужны остальным тестам —
    # вернуть их после усечения таблиц.
    serialized_rollback = True

    @classmethod
    def _fixture_setup(cls):
        # Другой TransactionTestCase, прошедший раньше (например, из
        # warehouse), при очистке базы заново создаёт content types и права
        # сигналом post_migrate — с НОВЫМИ id. Откат сериализованного снимка
        # (serialized_rollback) потом кладёт те же (app_label, model) со
        # СТАРЫМИ id и падает на уникальности django_content_type. Освобождаем
        # место заранее: снимок вернёт эти же строки (права уйдут каскадом и
        # вернутся вместе с ними), так что результат не зависит от порядка
        # тестов (--shuffle) и от соседей.
        ContentType.objects.all().delete()
        super()._fixture_setup()

    def tearDown(self):
        migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_forward_fills_without_moving_the_past_and_backward_undoes_it(self):
        apps = migrate(BEFORE)
        Kind = apps.get_model("finance", "ExpenseKind")
        Entry = apps.get_model("finance", "ExpenseEntry")
        Receipt = apps.get_model("sales", "Receipt")

        rent = Kind.objects.get(code="RENT")
        equipment = Kind.objects.get(code="EQUIPMENT")
        hidden = Kind.objects.create(
            code="справка", name="Справка", block="VARIABLE", in_profit=False,
        )
        kinds_before = Kind.objects.count()
        rent_entry = Entry.objects.create(kind=rent, amount=Decimal("25000"), spent_at=date(2026, 9, 5))
        machine = Entry.objects.create(kind=equipment, amount=Decimal("300000"), spent_at=date(2026, 9, 10))
        drill = Entry.objects.create(kind=equipment, amount=Decimal("8000"), spent_at=date(2026, 9, 11))
        noon = timezone.make_aware(datetime.combine(date(2026, 9, 15), time(12)))
        cash = Receipt.objects.create(order_number=1, payment_method="CASH",
                                      payment_status="PAID", stock_deducted=True, created_at=noon)
        online_paid = Receipt.objects.create(order_number=2, payment_method="ONLINE",
                                             payment_status="PAID", stock_deducted=True, created_at=noon)
        online_unpaid = Receipt.objects.create(order_number=3, payment_method="ONLINE",
                                               payment_status="PENDING", created_at=noon)

        apps = migrate(AFTER)
        Kind = apps.get_model("finance", "ExpenseKind")
        Entry = apps.get_model("finance", "ExpenseEntry")
        TaxRate = apps.get_model("finance", "TaxRate")
        Receipt = apps.get_model("sales", "Receipt")

        roles = dict(Kind.objects.values_list("code", "role"))
        self.assertEqual(roles["RENT"], "OPEX")
        self.assertEqual(roles["EQUIPMENT"], "CAPEX")
        self.assertEqual(roles["MATERIAL_PURCHASE"], "INVENTORY")
        self.assertEqual(roles["MATERIAL_DEBT"], "NOT_CASH")
        self.assertEqual(roles["INTEREST"], "INTEREST")
        self.assertEqual(roles["TAX"], "TAX")
        # Устаревший флаг миграция не трогает — его вернёт откат как был.
        self.assertFalse(Kind.objects.get(pk=hidden.pk).in_profit)

        self.assertEqual(Entry.objects.get(pk=rent_entry.pk).period, date(2026, 9, 1))
        self.assertEqual(Entry.objects.get(pk=machine.pk).useful_life_months, 60)
        self.assertIsNone(Entry.objects.get(pk=drill.pk).useful_life_months)

        self.assertEqual(
            list(TaxRate.objects.values_list("valid_from", "rate")),
            [(date(2026, 10, 1), Decimal("4.00"))],
        )

        self.assertEqual(Receipt.objects.get(pk=cash.pk).revenue_recognized_at, noon)
        self.assertEqual(Receipt.objects.get(pk=online_paid.pk).revenue_recognized_at, noon)
        self.assertIsNone(Receipt.objects.get(pk=online_unpaid.pk).revenue_recognized_at)

        # Назад — как не было.
        apps = migrate(BEFORE)
        Kind = apps.get_model("finance", "ExpenseKind")
        Entry = apps.get_model("finance", "ExpenseEntry")
        Receipt = apps.get_model("sales", "Receipt")
        field_names = {f.name for f in Entry._meta.get_fields()}
        self.assertNotIn("period", field_names)
        self.assertNotIn("role", {f.name for f in Kind._meta.get_fields()})
        self.assertNotIn("revenue_recognized_at", {f.name for f in Receipt._meta.get_fields()})
        self.assertEqual(Kind.objects.count(), kinds_before)
        self.assertFalse(Kind.objects.filter(code__in=["TAX", "INTEREST"]).exists())
        self.assertEqual(Entry.objects.count(), 3)
        self.assertFalse(Kind.objects.get(pk=hidden.pk).in_profit)

        # И снова вперёд — повторное применение не спотыкается.
        apps = migrate(AFTER)
        self.assertEqual(apps.get_model("finance", "TaxRate").objects.count(), 1)

    def test_backward_stops_when_new_kinds_already_have_entries(self):
        apps = migrate(AFTER)
        Kind = apps.get_model("finance", "ExpenseKind")
        Entry = apps.get_model("finance", "ExpenseEntry")
        Entry.objects.create(kind=Kind.objects.get(code="TAX"), amount=Decimal("100"),
                             spent_at=date(2026, 10, 20), period=date(2026, 10, 1))
        with self.assertRaisesMessage(RuntimeError, "уже есть траты"):
            migrate(BEFORE)
