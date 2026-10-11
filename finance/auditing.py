"""Записи журнала действий «было → стало» для денежных правок (XL-07, F3, STAFF-06).

Журнал был текстом без структуры: «Изменены реквизиты», а что именно и на что,
не видно. Здесь одно место, где собирается строка «поле: было → стало» и
пишется запись с типом (по типу журнал фильтруется).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from audit.models import AuditLog


def fmt(value) -> str:
    """Значение для журнала: деньги с пробелом-разделителем и запятой
    («12 000,50», без «,00»), даты по-русски (RU-N22: раньше «12 000.50»)."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, Decimal):
        text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
        return text[:-3] if text.endswith(",00") else text
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def changes(before: dict, after: dict, labels: dict) -> str:
    """«сумма 100 → 120; дата 05.10.2026 → 07.10.2026» — только то, что изменилось."""
    parts = []
    for key, label in labels.items():
        if key in before and key in after and before[key] != after[key]:
            parts.append(f"{label} {fmt(before[key])} → {fmt(after[key])}")
    return "; ".join(parts)


def snapshot(obj, fields) -> dict:
    return {name: getattr(obj, name) for name in fields}


def record(user, text: str, kind: str):
    return AuditLog.record(user, text, kind=kind)
