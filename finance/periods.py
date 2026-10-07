"""Проверка закрытого периода — одна на всю систему.

Правило простое и то же, что в 1С: **документ, лежащий в закрытом периоде, не
создаётся, не правится и не удаляется**. Иначе отчёт за месяц, который владелец
уже посмотрел и принял, назавтра показывает другую цифру, а объяснить её можно
только по журналу действий.

Ошибка — обычная `ValidationError` DRF: тогда любой эндпоинт отвечает 400 с
человеческим текстом, и не нужно ловить своё исключение в каждой вьюхе.
"""
from __future__ import annotations

import calendar
from datetime import date

from django.utils import timezone
from rest_framework.exceptions import ValidationError


# --- Месяцы и местный день ------------------------------------------------------
#
# Отчёты ОПиУ/ОДДС живут месяцами, а моменты в базе — в UTC. Граница суток и
# месяца везде одна — местная (`Asia/Bishkek`, `settings.TIME_ZONE`), и считать
# её нужно здесь, а не каждый раз заново: полночь по Бишкеку — вчерашний вечер по
# UTC, и самодельное `.date()` у момента уводит заказ в соседний месяц.


def local_day(value) -> date | None:
    """Момент или дата → местная дата. None остаётся None."""
    if value is None:
        return None
    if isinstance(value, str):            # '2026-10-15' из запроса или теста
        return date.fromisoformat(value[:10])
    if hasattr(value, "tzinfo"):          # datetime
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date()
    return value


def month_start(value) -> date | None:
    """Первое число месяца, в который попадает дата или момент."""
    day = local_day(value)
    return day.replace(day=1) if day else None


def month_end(value) -> date | None:
    """Последнее число месяца."""
    day = local_day(value)
    if day is None:
        return None
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def add_months(value, n: int) -> date:
    """Первое число месяца, отстоящего на `n` месяцев (n может быть < 0)."""
    first = month_start(value)
    index = first.year * 12 + (first.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def months_between(first, last) -> int:
    """Сколько месяцев от месяца `first` до месяца `last` включительно
    (0, если `last` раньше `first`)."""
    a, b = month_start(first), month_start(last)
    count = (b.year - a.year) * 12 + (b.month - a.month) + 1
    return max(count, 0)


def parse_month(value) -> date | None:
    """'2026-10' или '2026-10-15' (или date) → первое число месяца; мусор → None."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return month_start(value)
    text = str(value).strip()
    try:
        if len(text) == 7:
            return date.fromisoformat(f"{text}-01")
        return month_start(date.fromisoformat(text[:10]))
    except ValueError:
        return None


class PeriodClosed(ValidationError):
    """Операция попала в закрытый период."""


def closed_through():
    """Дата, по которую всё закрыто. None — период открыт."""
    from .models import PeriodLock

    return PeriodLock.load().closed_through


def is_closed(day) -> bool:
    if day is None:
        return False
    limit = closed_through()
    return bool(limit and day <= limit)


def ensure_open(day, what="Эту операцию"):
    """Пустить дальше, только если дата не в закрытом периоде.

    `day` может быть `date` или `datetime` — второе приводим к местной дате:
    у заказа дата хранится моментом, и сравнивать её с датой закрытия по UTC
    значило бы закрывать день не тогда, когда его закрыли.
    """
    if day is None:
        return
    value = local_day(day)
    limit = closed_through()
    if limit and value <= limit:
        raise PeriodClosed(
            f"{what} нельзя: период закрыт по {limit.strftime('%d.%m.%Y')}. "
            "Чтобы поправить, откройте период в Финансах."
        )


def ensure_month_open(value, what="Эту операцию"):
    """Пустить дальше, только если ВЕСЬ месяц открыт.

    Для того, что ложится в ОПиУ месяцем целиком, а не днём: «за какой месяц»
    у траты, месяц остановки амортизации, начало действия ставки налога. Замок
    «по 15.10» уже закрыл часть октября — менять октябрьскую прибыль нельзя.
    """
    ensure_open(month_start(value), what)
