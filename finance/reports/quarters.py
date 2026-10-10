"""Колонки «Квартал» годовых таблиц ОПиУ, ОДДС и сверки (G2-N1).

Квартал — сумма трёх месяцев; исключения у строк, которые суммировать нельзя:
проценты считаются от сумм квартала (не суммой процентов), остаток на начало —
это остаток первого месяца квартала, остаток на конец — последнего.
"""
from __future__ import annotations

from .money import pct, total


def quarter_meta(months) -> list[dict]:
    """Подписи кварталов: `future` — все три месяца ещё впереди."""
    out = []
    for q in range(4):
        part = months[q * 3:q * 3 + 3]
        out.append({
            "quarter": q + 1,
            "future": all(m["future"] for m in part),
            "partial": any(m["future"] for m in part) and not all(m["future"] for m in part),
        })
    return out


def add_quarters(rows: list[dict], *, percent: dict | None = None, first=(), last=()) -> list[dict]:
    """Каждой строке — `quarters`: четыре значения.

    `percent` — {ключ строки-процента: (ключ числителя, ключ знаменателя)};
    `first` / `last` — ключи строк-остатков (берётся первый / последний месяц)."""
    percent = percent or {}
    by_key = {r["key"]: r for r in rows}

    def chunk(values, q):
        return values[q * 3:q * 3 + 3]

    for row in rows:
        key = row["key"]
        if key in percent:
            num, den = by_key[percent[key][0]], by_key[percent[key][1]]
            row["quarters"] = [
                pct(total(chunk(num["values"], q)), total(chunk(den["values"], q))) for q in range(4)
            ]
        elif key in first or row.get("kind") == "balance":
            row["quarters"] = [chunk(row["values"], q)[0] for q in range(4)]
        elif key in last or key == "closing" or key.startswith("closing:"):
            row["quarters"] = [chunk(row["values"], q)[-1] for q in range(4)]
        else:
            row["quarters"] = [total(chunk(row["values"], q)) for q in range(4)]
    return rows
