"""«Что если» для ОПиУ (PNL-09, G2-N4): пересчёт года без записи в базу.

Два движка, как две ячейки в Excel владельца:

- «цена услуги × (1 + x %)» — выручка от УСЛУГ (резка, гравировка, монтаж…),
  материал не трогается;
- «закуп × (1 + y %)» — себестоимость проданного и потери материала.

Расходы месяца, амортизация и проценты остаются прежними. Налог пересчитывается
по ставке месяца с изменения выручки (при основе «по кассе» считаем, что та же
доля денег дойдёт до кассы — прогноз, а не учёт). Ничего не пишется: функция
только читает и считает.

Проценты мастеров (PNL-09, D-188). Мастер получает процент от стоимости работы
— цена услуг выше, выше и его зарплата: в Excel владельца +10 % к услугам
октября поднимали проценты на 179 сом. Процент строки — по правилам ведомости
её исполнителя на месяц (`finance.payroll`: исполнитель строки, учётка
кассира, единственный за станком); строка без исполнителя или исполнитель без
правил — по средней ставке месяца (взвешенной по сумме работ), месяц без
правил — по средней ставке года. Премия за выработку не пересчитывается.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

from ..periods import month_end
from .money import ZERO, pct, q2, total
from .pnl import pnl, tax_rate_for
from .scope import report_scope

# Допустимая вилка процентов: от «цены нет» до «цена выросла в 11 раз».
LOW, HIGH = Decimal("-100"), Decimal("1000")


def clamp(value) -> Decimal:
    return max(LOW, min(HIGH, Decimal(value or 0)))


def master_pay(d_from, d_to, directory=None) -> tuple[Decimal, Decimal, Decimal]:
    """(процентами по правилам, работы с правилами, работы без правил) за период.

    Гарантийная переделка — не выработка (как в ведомости, D-161)."""
    from .. import payroll
    from .work import service_lines

    directory = directory or payroll.Directory()
    known_pay = known_base = unknown_base = ZERO
    for sign, line, _day in service_lines(d_from, d_to):
        work = payroll.work_of(line)
        if work is None or line.receipt.is_warranty:
            continue
        # Процент мастера — от работы без срочности (D-197), как в ведомости.
        amount = sign * payroll.work_amount(line)
        executor = directory.executor_of(line)
        rates = directory.rates(executor, d_from) if executor else None
        if rates is None:
            unknown_base += amount
            continue
        known_base += amount
        known_pay += amount * payroll._rate_for(rates, work) / 100
    return known_pay, known_base, unknown_base


@report_scope
def what_if_year(year: int, price_pct=0, cost_pct=0) -> dict:
    price_pct, cost_pct = clamp(price_pct), clamp(cost_pct)
    today = timezone.localdate()
    months = [(date(year, m, 1), month_end(date(year, m, 1))) for m in range(1, 13)]
    base = {k: [] for k in ("revenue", "cogs", "gross", "masters", "ebitda", "operating", "tax", "net")}
    scen = {k: [] for k in base}
    from .. import payroll

    directory = payroll.Directory()
    work = [master_pay(first, last, directory) for first, last in months]
    year_known = sum((k for _p, k, _u in work), ZERO)
    year_avg = sum((p for p, _k, _u in work), ZERO) / year_known if year_known else ZERO
    for (first, last), (known_pay, known_base, unknown_base) in zip(months, work):
        p = pnl(first, last)
        service_revenue = p["revenue_cutting"] + p["revenue_other"]
        d_rev = q2(service_revenue * price_pct / 100)
        d_cogs = q2(p["cogs_total"] * cost_pct / 100)
        d_tax = q2(tax_rate_for(first) * d_rev / 100)
        avg = known_pay / known_base if known_base else year_avg
        masters = q2(known_pay + unknown_base * avg)
        d_pay = q2(masters * price_pct / 100)
        base["revenue"].append(p["revenue"])
        base["cogs"].append(p["cogs_total"])
        base["gross"].append(p["gross_profit"])
        base["masters"].append(masters)
        base["ebitda"].append(p["ebitda"])
        base["operating"].append(p["operating_profit"])
        base["tax"].append(p["tax"])
        base["net"].append(p["net_profit"])
        scen["revenue"].append(p["revenue"] + d_rev)
        scen["cogs"].append(p["cogs_total"] + d_cogs)
        scen["gross"].append(p["gross_profit"] + d_rev - d_cogs)
        scen["masters"].append(masters + d_pay)
        scen["ebitda"].append(p["ebitda"] + d_rev - d_cogs - d_pay)
        scen["operating"].append(p["operating_profit"] + d_rev - d_cogs - d_pay)
        scen["tax"].append(p["tax"] + d_tax)
        scen["net"].append(p["net_profit"] + d_rev - d_cogs - d_pay - d_tax)

    labels = [
        ("revenue", "Выручка"), ("cogs", "Себестоимость"), ("gross", "Валовая прибыль"),
        ("masters", "Проценты мастеров"),
        ("ebitda", "EBITDA"), ("operating", "Операционная прибыль"), ("tax", "Налог"),
        ("net", "Чистая прибыль"),
    ]
    rows = []
    for key, label in labels:
        b, s = base[key], scen[key]
        rows.append({
            "key": key, "label": label,
            "base": b, "scenario": s, "delta": [x - y for x, y in zip(s, b)],
            "base_total": total(b), "scenario_total": total(s), "delta_total": total(s) - total(b),
        })
    revenue_b, revenue_s = total(base["revenue"]), total(scen["revenue"])
    return {
        "year": year,
        "price_pct": price_pct,
        "cost_pct": cost_pct,
        "months": [
            {"month": first.month, "from": first, "to": last, "future": first > today}
            for first, last in months
        ],
        "rows": rows,
        "margin": {
            "base": pct(total(base["net"]), revenue_b),
            "scenario": pct(total(scen["net"]), revenue_s),
        },
        "assumptions": (
            "Цена услуг и закуп меняются на указанные проценты; проценты мастеров растут вместе "
            "с ценой услуг (по правилам ведомости, без правил — по средней ставке); материал в "
            "выручке, остальные расходы, амортизация и проценты по займам не меняются; налог "
            "пересчитан по ставке месяца. Это расчёт без записи в базу."
        ),
    }
