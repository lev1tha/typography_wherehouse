"""«Что если» для ОПиУ (PNL-09, G2-N4): пересчёт года без записи в базу.

Два движка, как две ячейки в Excel владельца:

- «цена услуги × (1 + x %)» — выручка от УСЛУГ (резка, гравировка, монтаж…),
  материал не трогается;
- «закуп × (1 + y %)» — себестоимость проданного и потери материала.

Расходы месяца, амортизация и проценты остаются прежними. Налог пересчитывается
по ставке месяца с изменения выручки (при основе «по кассе» считаем, что та же
доля денег дойдёт до кассы — прогноз, а не учёт). Ничего не пишется: функция
только читает и считает.
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


@report_scope
def what_if_year(year: int, price_pct=0, cost_pct=0) -> dict:
    price_pct, cost_pct = clamp(price_pct), clamp(cost_pct)
    today = timezone.localdate()
    months = [(date(year, m, 1), month_end(date(year, m, 1))) for m in range(1, 13)]
    base = {k: [] for k in ("revenue", "cogs", "gross", "ebitda", "operating", "tax", "net")}
    scen = {k: [] for k in base}
    for first, last in months:
        p = pnl(first, last)
        service_revenue = p["revenue_cutting"] + p["revenue_other"]
        d_rev = q2(service_revenue * price_pct / 100)
        d_cogs = q2(p["cogs_total"] * cost_pct / 100)
        d_tax = q2(tax_rate_for(first) * d_rev / 100)
        base["revenue"].append(p["revenue"])
        base["cogs"].append(p["cogs_total"])
        base["gross"].append(p["gross_profit"])
        base["ebitda"].append(p["ebitda"])
        base["operating"].append(p["operating_profit"])
        base["tax"].append(p["tax"])
        base["net"].append(p["net_profit"])
        scen["revenue"].append(p["revenue"] + d_rev)
        scen["cogs"].append(p["cogs_total"] + d_cogs)
        scen["gross"].append(p["gross_profit"] + d_rev - d_cogs)
        scen["ebitda"].append(p["ebitda"] + d_rev - d_cogs)
        scen["operating"].append(p["operating_profit"] + d_rev - d_cogs)
        scen["tax"].append(p["tax"] + d_tax)
        scen["net"].append(p["net_profit"] + d_rev - d_cogs - d_tax)

    labels = [
        ("revenue", "Выручка"), ("cogs", "Себестоимость"), ("gross", "Валовая прибыль"),
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
            "Цена услуг и закуп меняются на указанные проценты; материал в выручке, расходы, "
            "амортизация и проценты не меняются; налог пересчитан по ставке месяца. "
            "Это расчёт без записи в базу."
        ),
    }
