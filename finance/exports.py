"""CSV-выгрузки «в Excel»: разделитель «;», BOM, деньги с запятой.

Так русский Excel открывает файл сразу по колонкам, без кракозябр, и видит
числа числами: «1234,50», а не текст «1234.50». Даты — ДД.ММ.ГГГГ.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal

from django.http import HttpResponse


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, Decimal):
        text = format(value.quantize(Decimal("0.01")), "f")
        return text.replace(".", ",")
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def csv_response(rows, filename: str) -> HttpResponse:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    for row in rows:
        writer.writerow([cell(v) for v in row])
    response = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
