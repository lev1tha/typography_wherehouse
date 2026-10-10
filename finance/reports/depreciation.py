"""График амортизации капвложений (D-1, D-13, D-20, D-21).

Покупка вида «Инвестиции» не дешевле порога — актив (`ExpenseEntry.useful_life_months`
задан). Её цена попадает в прибыль равными долями:

- со СЛЕДУЮЩЕГО месяца после покупки (месяц покупки — без амортизации);
- доля = цена / срок вниз до тыйына, последний месяц добирает остаток, так что
  сумма долей ТОЧНО равна цене (`money.split_evenly`);
- выбытие (`depreciate_until`): по этот месяц включительно обычная доля, в нём же
  разом списывается остаток стоимости, дальше — ничего. Выбытие раньше начала
  графика — вся цена списывается в месяце выбытия.

Итого за жизнь актива в ОПиУ попадает ровно цена покупки — обычной амортизацией
и, если было выбытие, списанием остатка.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from ..models import ExpenseEntry, ExpenseKind
from ..periods import add_months, month_start
from .money import ZERO, split_evenly
from .scope import once


def start_month(entry) -> date:
    """Первый месяц амортизации — следующий за месяцем покупки."""
    return add_months(entry.spent_at, 1)


def schedule(entry) -> dict[date, tuple]:
    """{месяц: (обычная доля, списание при выбытии)} одной покупки."""
    if not entry.useful_life_months:
        return {}
    start = start_month(entry)
    until = month_start(entry.depreciate_until) if entry.depreciate_until else None
    out = {}
    charged = ZERO
    for i, share in enumerate(split_evenly(entry.amount, entry.useful_life_months)):
        month = add_months(start, i)
        if until is not None and month > until:
            break
        out[month] = (share, ZERO)
        charged += share
    if until is not None:
        residual = entry.amount - charged
        if residual:
            regular = out.get(until, (ZERO, ZERO))[0]
            out[until] = (regular, residual)
    return out


def capitalized_entries():
    return ExpenseEntry.objects.filter(
        kind__role=ExpenseKind.Role.CAPEX, useful_life_months__isnull=False,
    ).select_related("kind")


def _capitalized_before(day):
    """Амортизируемые покупки до даты. Список читается один раз на отчёт."""
    rows = once("capitalized_entries", lambda: list(capitalized_entries()))
    return [e for e in rows if e.spent_at < day]


def by_month(first_month: date, last_month: date) -> dict[date, dict]:
    """{месяц: {"depreciation": …, "disposal": …}} по всем активам за месяцы
    от `first_month` до `last_month` включительно."""
    first, last = month_start(first_month), month_start(last_month)
    out = defaultdict(lambda: {"depreciation": ZERO, "disposal": ZERO})
    for entry in _capitalized_before(add_months(last, 1)):
        for month, (regular, disposal) in schedule(entry).items():
            if first <= month <= last:
                out[month]["depreciation"] += regular
                out[month]["disposal"] += disposal
    return out
