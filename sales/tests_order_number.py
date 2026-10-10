"""Номер чека: проигравший гонку берёт следующий номер, а не получает 500.

`Receipt.save` берёт `Max(order_number) + 1`. Два кассира, оформляющие
одновременно, видели один и тот же Max; unique-ограничение не давало записать
второго — и он падал `IntegrityError` → 500 (из четырёх параллельных оформлений
падали три). Теперь вставка в точке сохранения повторяется с новым номером.
Параллельный сценарий на настоящей базе — в `tests_race_guards.py`.
"""
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase

from sales.models import Receipt


class OrderNumberRetryTests(TestCase):
    def test_collision_takes_the_next_number(self):
        Receipt.objects.create()  # №1
        real = Receipt.objects.aggregate
        calls = []

        def stale_then_real(*args, **kwargs):
            # Первый раз «читаем» Max до чужого коммита — как проигравший гонку.
            calls.append(1)
            if len(calls) == 1:
                return {"m": 0}
            return real(*args, **kwargs)

        with mock.patch.object(Receipt.objects, "aggregate", side_effect=stale_then_real):
            second = Receipt.objects.create()
        self.assertEqual(second.order_number, 2)
        self.assertEqual(len(calls), 2)

    def test_numbering_after_deleting_the_last_receipt_is_unchanged(self):
        """Прежнее правило: номер последнего удалённого чека берётся заново."""
        Receipt.objects.create()
        last = Receipt.objects.create()
        self.assertEqual(last.order_number, 2)
        last.delete()
        self.assertEqual(Receipt.objects.create().order_number, 2)

    def test_foreign_integrity_error_is_not_swallowed(self):
        """Падение не из-за номера (номер свободен) — настоящая ошибка, её не прячем."""
        with mock.patch(
            "django.db.models.Model.save", side_effect=IntegrityError("не про номер")
        ):
            with self.assertRaises(IntegrityError):
                Receipt().save()
