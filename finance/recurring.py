"""Повторяющиеся траты (PNL-09, G2-N4): «аренда 25 000 каждого 10-го».

Правило (`RecurringExpense`) порождает обычные траты по расписанию. Когда это
происходит:

- командой `manage.py generate_recurring` (удобно поставить в cron раз в сутки);
- при открытии «Финансов» — интерфейс вызывает `POST /api/finance/recurring/run/`
  (отчёты при чтении ничего не пишут).

Правила вывода:

- по трате на месяц, от `start_month` до `until_month` включительно (нет конца —
  до текущего месяца); число — `day`, в коротком месяце последний день;
- трата вносится, когда её день НАСТУПИЛ: будущие не пишутся;
- месяц уже внесён, если есть трата с этой ссылкой и этим «за какой месяц»:
  повторный запуск ничего не задваивает, а ручное удаление траты месяц не
  «воскрешает» только пока правило не запущено заново — удалённую трату
  расписание создаст снова; чтобы остановить, меняют «по месяц» или выключают
  правило;
- закрытый замком период пропускается и попадает в ответ списком, а не ошибкой;
- не больше 60 месяцев за один запуск на правило.
"""
from __future__ import annotations

import calendar
from datetime import date

from django.db import transaction
from django.utils import timezone

from . import auditing, cash
from .models import ExpenseEntry, RecurringExpense
from .periods import add_months, is_closed, month_start

MAX_MONTHS_PER_RUN = 60


def day_in(month: date, day: int) -> date:
    """Число `day` месяца; в коротком месяце — последний день."""
    last = calendar.monthrange(month.year, month.month)[1]
    return date(month.year, month.month, min(max(day, 1), last))


def due_months(rule: RecurringExpense, today: date) -> list[date]:
    """Месяцы, за которые трату пора вносить (день наступил, срок не вышел)."""
    last = month_start(today)
    if rule.until_month:
        last = min(last, rule.until_month)
    months, month = [], rule.start_month
    while month <= last and len(months) < MAX_MONTHS_PER_RUN:
        if day_in(month, rule.day) <= today:
            months.append(month)
        month = add_months(month, 1)
    return months


@transaction.atomic
def generate(today: date | None = None, user=None) -> dict:
    """Завести недостающие траты по всем действующим правилам."""
    today = today or timezone.localdate()
    created, skipped = [], []
    for rule in RecurringExpense.objects.filter(is_active=True).select_related("kind"):
        have = set(
            ExpenseEntry.objects.filter(recurring=rule).values_list("period", flat=True)
        )
        for month in due_months(rule, today):
            if month in have:
                continue
            day = day_in(month, rule.day)
            if is_closed(day) or is_closed(month_start(month)):
                skipped.append({"rule": rule.id, "month": month, "reason": "closed"})
                continue
            entry = ExpenseEntry.objects.create(
                kind=rule.kind, name=rule.name, amount=rule.amount, account=rule.account,
                spent_at=day, period=month, note=rule.note or "По расписанию",
                recurring=rule, created_by=user or rule.created_by,
            )
            cash.sync_expense(entry, user=user or rule.created_by)
            created.append({"rule": rule.id, "month": month, "entry": entry.id, "amount": entry.amount})
    if created:
        auditing.record(
            user,
            "Повторяющиеся траты: внесено " + str(len(created)) + " — "
            + ", ".join(f"{c['month']:%m.%Y} {auditing.fmt(c['amount'])}" for c in created[:6])
            + ("…" if len(created) > 6 else ""),
            "expense",
        )
    return {"created": created, "skipped": skipped}
