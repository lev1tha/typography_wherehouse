"""Прибыль по дням одного месяца — график под «Сводкой».

Итог месяца прячет, что один день был провальным. Каждый день — та же
арифметика, что ОПиУ (`pnl.pnl_by_day`): выручка и себестоимость своим днём,
потери — днём записи склада, операционные расходы по «за какой месяц» (оплачен в
своём месяце — днём оплаты, постоянные — поровну по дням), амортизация и
проценты — поровну, налог — от выручки дня. Поэтому сумма дней РАВНА чистой
прибыли месяца в «Сводке» и ОПиУ, а не «почти».
"""
from __future__ import annotations

from datetime import date

from django.utils import timezone

from ..periods import month_end
from .money import total
from .pnl import pnl_by_day

FIELDS = ("revenue", "cogs", "losses", "opex", "opex_cash_manual", "cash_count",
          "depreciation", "interest", "tax", "net_profit")


def daily_report(year: int, month: int) -> dict:
    today = timezone.localdate()
    first = date(year, month, 1)
    last = month_end(first)
    days = pnl_by_day(first, last)

    rows = []
    for d in days:
        # Будущему дню нечего показывать: его доля аренды ещё не «отработана»,
        # и столбик краснел бы раньше, чем день начался.
        future = d["date"] > today
        rows.append({
            "date": d["date"].isoformat(),
            "day": d["date"].day,
            **{f: d[f] for f in FIELDS if f != "net_profit"},
            "profit": None if future else d["net_profit"],
        })

    # ИТОГ — за МЕСЯЦ ЦЕЛИКОМ (сумма всех дней, включая будущие доли аренды и
    # амортизации): та же цифра, что чистая прибыль месяца в «Сводке».
    totals = {f: total(d[f] for d in days) for f in FIELDS}
    totals["profit"] = totals.pop("net_profit")
    # Для подписи под графиком: «расходы» — операционные расходы месяца (та
    # же цифра, что плитка «Расходы» «Сводки», с поправкой на пересчёт
    # кассы), «ниже EBITDA» — амортизация, проценты и налог.
    totals["expenses"] = totals["opex"] + totals["opex_cash_manual"] - totals["cash_count"]
    totals["below"] = totals["depreciation"] + totals["interest"] + totals["tax"]
    return {
        "year": year,
        "month": month,
        "days_in_month": last.day,
        "today": today.isoformat() if (today.year == year and today.month == month) else None,
        "rows": rows,
        "totals": totals,
    }
