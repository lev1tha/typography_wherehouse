"""Проверка закрытого периода — одна на всю систему.

Правило простое и то же, что в 1С: **документ, лежащий в закрытом периоде, не
создаётся, не правится и не удаляется**. Иначе отчёт за месяц, который владелец
уже посмотрел и принял, назавтра показывает другую цифру, а объяснить её можно
только по журналу действий.

Ошибка — обычная `ValidationError` DRF: тогда любой эндпоинт отвечает 400 с
человеческим текстом, и не нужно ловить своё исключение в каждой вьюхе.
"""
from __future__ import annotations

from rest_framework.exceptions import ValidationError


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
    value = day.date() if hasattr(day, "date") else day
    limit = closed_through()
    if limit and value <= limit:
        raise PeriodClosed(
            f"{what} нельзя: период закрыт по {limit.strftime('%d.%m.%Y')}. "
            "Чтобы поправить, откройте период в Финансах."
        )
