"""Снимок склада на конец дня (STK-04, волна 2).

    python manage.py stock_snapshot                 # на конец прошлого месяца
    python manage.py stock_snapshot --date 2026-09-30
    python manage.py stock_snapshot --month 2026-09

Удобно поставить в cron на последний день месяца в 23:55 — тогда снимок точный:
партии ещё не тронуты следующим месяцем. Пересъёмка той же даты заменяет снимок.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from finance.periods import month_end, parse_month
from warehouse.snapshots import take_snapshot


class Command(BaseCommand):
    help = "Снять снимок остатков партий на конец дня (по умолчанию — конец прошлого месяца)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="ГГГГ-ММ-ДД — на конец этого дня")
        parser.add_argument("--month", help="ГГГГ-ММ — на последний день месяца")

    def handle(self, *args, **opts):
        if opts.get("date"):
            try:
                day = date.fromisoformat(opts["date"])
            except ValueError as e:
                raise CommandError("Дата — в виде ГГГГ-ММ-ДД.") from e
        elif opts.get("month"):
            first = parse_month(opts["month"])
            if first is None:
                raise CommandError("Месяц — в виде ГГГГ-ММ.")
            day = month_end(first)
        else:
            day = timezone.localdate().replace(day=1) - timedelta(days=1)
        if day > timezone.localdate():
            raise CommandError("Снимок будущего дня снять нельзя.")
        snap = take_snapshot(day, source="command")
        self.stdout.write(
            f"Снимок склада на {day:%d.%m.%Y}: {snap.lines.count()} строк, {snap.value} сом"
        )
