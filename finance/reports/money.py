"""Деньги в отчётах: Decimal и одно правило округления.

ПРАВИЛО ОКРУГЛЕНИЯ (одно на все отчёты, 2026-10-07):

1. Суммы хранятся и считаются в `Decimal`, `float` не бывает нигде.
2. Деньги отчёта — до тыйына (0.01). Где получается дробь мельче (налог при
   нецелой ставке), округляем ПО-БУХГАЛТЕРСКИ — половина вверх (`q2`), и один
   раз: на итоге месяца, а не на каждой строке.
3. Когда сумму нужно РАЗЛОЖИТЬ на части (амортизация по месяцам, аренда по
   дням месяца, налог по дням), части считаются вниз до тыйына, а последняя
   часть добирает остаток (`split_evenly`, `cumulative_split`). Так сумма
   частей ВСЕГДА точно равна целому, и ни одна часть не уходит в минус.
4. Проценты (маржа) — до десятой, половина вверх (`pct`); от нулевой базы —
   None («нечего делить»), а не ноль.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce

ZERO = Decimal("0")
CENT = Decimal("0.01")
TENTH = Decimal("0.1")


def q2(value) -> Decimal:
    """До тыйына, половина вверх."""
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def down2(value) -> Decimal:
    """До тыйына вниз (для частей, которые потом добирает последняя)."""
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_DOWN)


def pct(part, whole) -> Decimal | None:
    """Доля в процентах до десятой. Нулевая база — None."""
    if not whole:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(TENTH, rounding=ROUND_HALF_UP)


def split_evenly(amount, parts: int) -> list[Decimal]:
    """Разложить сумму на `parts` равных частей до тыйына; последняя добирает.

    Отрицательная сумма раскладывается зеркально. `parts < 1` — пусто.
    """
    if parts < 1:
        return []
    amount = Decimal(amount or 0)
    sign = -1 if amount < 0 else 1
    total = abs(amount)
    share = down2(total / parts)
    out = [share] * (parts - 1) + [total - share * (parts - 1)]
    return [sign * x for x in out]


def cumulative_split(values: list) -> list[Decimal]:
    """Округлить ряд так, чтобы сумма округлённых = округлённой сумме.

    Каждая часть — разность округлённых накопленных сумм (`q2`). Нужен налогу
    по дням: налог месяца = q2(ставка × выручка месяца), а дневные доли в
    сумме дают ровно его.
    """
    out, running, prev = [], ZERO, ZERO
    for v in values:
        running += Decimal(v or 0)
        rounded = q2(running)
        out.append(rounded - prev)
        prev = rounded
    return out


def total(values) -> Decimal:
    return sum((Decimal(v or 0) for v in values), ZERO)


def SUM(field):
    """Сумма поля в базе; пусто — ноль, а не None."""
    return Coalesce(Sum(field), ZERO, output_field=DecimalField())
