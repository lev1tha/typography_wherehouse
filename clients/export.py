"""CSV списка клиентов и должников — открывается в русском Excel сразу по колонкам.

Разделитель «;», дробная часть через запятую, BOM в начале (иначе кириллица
превращается в кракозябры). Те же числа, что на экране: сальдо, давность долга,
последний заказ; маржа — только для тех, кто видит закупку.
"""
from __future__ import annotations

import csv
import io
import re
from decimal import Decimal

from django.http import HttpResponse

from .analytics import age_days, local_date

BOM = "﻿"


def money(value) -> str:
    return f"{Decimal(value or 0):.2f}".replace(".", ",")


def day(moment) -> str:
    d = local_date(moment)
    return d.strftime("%d.%m.%Y") if d else ""


def safe(text) -> str:
    """Ячейка, которую Excel не примет за формулу (=, @, табуляция; +/- перед не-числом)."""
    value = "" if text is None else str(text)
    if value[:1] in ("=", "@", "\t", "\r"):
        return "'" + value
    if value[:1] in ("+", "-") and not re.fullmatch(r"[+\-]?[\d\s()\-]+", value):
        return "'" + value
    return value


def clients_csv(queryset, *, with_margin: bool) -> HttpResponse:
    header = [
        "Клиент", "Тип", "Телефон", "Заказов", "Долг, сом", "Сдача, сом", "Аванс, сом",
        "Сальдо, сом", "Старейший долг с", "Дней долга", "Последний заказ",
        "Дней с последнего заказа", "Лимит долга, сом", "Скидка, %",
    ]
    if with_margin:
        header.append("Маржа, сом")
    from .models import ClientSettings

    default_limit = ClientSettings.load().default_credit_limit
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    for c in queryset:
        limit = c.credit_limit if c.credit_limit is not None else default_limit
        row = [
            safe(c.display_name), "ОсОО" if c.type == "OSOO" else "Физ. лицо", safe(c.phone),
            c.orders_count, money(c.debt), money(c.change_due_total), money(c.advance_total),
            money(c.balance), day(c.oldest_debt_at), age_days(c.oldest_debt_at) if c.oldest_debt_at else "",
            day(c.last_order_at), age_days(c.last_order_at) if c.last_order_at else "",
            money(limit) if limit is not None else "", str(c.discount_percent).replace(".", ","),
        ]
        if with_margin:
            row.append(money(c.margin_total))
        writer.writerow(row)
    response = HttpResponse(BOM + buf.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="klienty.csv"'
    return response
