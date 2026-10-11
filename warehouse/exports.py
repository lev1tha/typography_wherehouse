"""Выгрузки склада «в Excel» (XL-06, STK-09; волна 2).

Формат тот же, что у отчётов (`finance/exports.py`): разделитель «;», BOM,
числа с запятой — русский Excel открывает файл сразу по колонкам и видит
числа числами. Количества — до 4 знаков (лист 2,9768 кв.м), деньги — 2.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal

from django.http import HttpResponse
from django.utils import timezone

from .models import SupplyLine

MONEY = Decimal("0.01")
QTY = Decimal("0.0001")


def num(value, places=MONEY) -> str:
    if value is None or value == "":
        return ""
    text = format(Decimal(value).quantize(places), "f")
    if places == QTY and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, Decimal):
        return num(value)
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%d.%m.%Y %H:%M") if timezone.is_aware(value) else value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return str(value)


def csv_response(rows, filename: str) -> HttpResponse:
    """Строки (уже готовые ячейки или значения) → CSV-ответ с BOM."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    for row in rows:
        writer.writerow([v if isinstance(v, str) else cell(v) for v in row])
    response = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def reorder_csv(rows: list[dict]):
    # «Заказывать до» — последней колонкой (S3, STK-06): порядок прежних не
    # меняется, файл, который уже разбирают в Excel, не съезжает.
    yield ["Материал", "Единица", "Остаток", "Минимум", "Заказать", "Поставщик", "Закуп за единицу",
           "Сумма", "Заказывать до"]
    for r in rows:
        yield [
            r["name"], r["unit_label"], num(r["stock"]), num(r["min"]), num(r["to_order"]),
            r["supplier"] or "", num(r["unit_cost"]), num(r["sum"]), num(r.get("target")),
        ]


def _form_label(m) -> str:
    if m.sells_by_metre:
        return "рулон"
    return "лист" if m.is_roll_material else "штучный"


def catalog_csv(materials, *, money: bool):
    """Каталог с остатками и ценами — та же таблица, что на экране, плюс
    остаток в единицах материала и стоимость склада."""
    from .reorder import min_in_units, stock_in_units, unit_label

    yield [
        "Название", "Тип", "Толщина, мм", "Цвет", "Артикул", "Форма", "Лист, м", "Остаток, ед. хранения",
        "Ед. хранения", "Остаток", "Ед.", "Минимум", "Закуп за ед. хранения", "Цена за кв.м",
        "Цена за лист/шт", "Цена за пог.м", "Опт за лист", "Опт от", "Резка за пог.м", "Наценка, %",
        "КИМ, %", "Стоимость склада",
    ]
    for m in materials:
        sheet = f"{num(m.sheet_width, QTY)}×{num(m.sheet_height, QTY)}" if m.sheet_width and m.sheet_height else ""
        yield [
            m.name, m.type.name if m.type_id else "", num(m.thickness_mm, QTY), m.color, m.article,
            _form_label(m), sheet, num(m.quantity, QTY), m.get_unit_display(),
            num(stock_in_units(m), QTY), unit_label(m), num(min_in_units(m), QTY),
            num(m.purchase_price) if money else "", num(m.price_per_sqm),
            num(m.piece_price if m.is_roll_material else m.price_per_unit), num(m.price_per_pm),
            num(m.wholesale_price), num(m.wholesale_min_qty, QTY), num(m.cut_rate_per_pm),
            num(m.markup_percent) if money else "", num(m.kim_percent),
            num(m.stock_value) if money else "",
        ]


def journal_csv(logs, *, money: bool):
    """Журнал движений склада."""
    yield ["Дата", "Операция", "Материал", "Изменение", "Ед.", "Метры", "Себестоимость",
           "Заказ", "Причина", "Кто"]
    for log in logs:
        m = log.material
        # Возврат поставщику — своя операция, как в «Движении» (RU-N23).
        supplier_return = (
            log.type == log.Type.CORRECTION and (log.reason or "").startswith("Возврат поставщику")
        )
        yield [
            cell(log.happened_at), "Возврат поставщику" if supplier_return else log.get_type_display(),
            m.name, num(log.quantity_changed, QTY),
            "кв.м" if m.is_roll_material else m.get_unit_display(),
            num(log.metres_changed, QTY), num(log.cost) if money else "",
            str(log.receipt.order_number or "") if log.receipt_id else "",
            log.reason or "", log.created_by.username if log.created_by_id else "",
        ]


def lots_csv(rolls, *, money: bool):
    """Партии: что пришло, сколько осталось, почём."""
    yield ["Партия", "Материал", "Форма", "Размеры", "Принято", "Остаток", "Ед.",
           "Закуп партии", "Цена за ед.", "Остаток по закупу", "Поступила", "Производство",
           "Накладная", "Долг поставщику"]
    for r in rolls:
        unit = r.material.get_unit_display() if r.form == "PIECE" else "кв.м"
        try:
            supply = r.supply_line.supply
        except SupplyLine.DoesNotExist:  # партия без накладной
            supply = None
        yield [
            r.code or f"№{r.pk}", r.material.name, r.get_form_display(), r.dimensions_label,
            num(r.initial_area, QTY), num(r.remaining_area, QTY), unit,
            num(r.purchase_cost) if money else "", num(r.cost_per_sqm) if money else "",
            num(r.cost_of(r.remaining_area)) if money else "", cell(r.received_at),
            r.production.name if r.production_id else "",
            (supply.number or f"#{supply.pk}") if supply else "",
            num(r.supplier_debt) if money else "",
        ]


def _text(value) -> str:
    """Текст ячейки без формулы: «=…», «+…», «@…» Excel выполнил бы."""
    text = str(value or "")
    return "'" + text if text[:1] in ("=", "+", "@", "-") and not text[1:2].isdigit() else text


def supplies_csv(supplies, *, money: bool, dupes: dict | None = None):
    """Список накладных (XL-06, перепроверка 10.10): тот же, что на экране, —
    с суммой, оплатой, долгом и возвратами. Складовщику суммы пустые (D-118)."""
    dupes = dupes or {}
    yield [
        "Дата", "Номер", "Поставщик", "Позиций", "Валюта", "Курс", "Сумма в валюте",
        "Сумма, сом", "Возвращено, сом", "Оплачено, сом", "Долг, сом", "Переплата, сом",
        "Сумма по бумаге", "Начальные остатки", "Похоже на дубль №", "Примечание", "Кто ввёл",
    ]
    for s in supplies:
        returned = sum((r.amount for r in s.returns.all()), Decimal("0"))
        dup = dupes.get(s.id)
        yield [
            cell(s.received_on), _text(s.number), _text(s.supplier.name if s.supplier_id else ""),
            str(len(s.lines.all())), s.currency, num(s.rate, QTY) if s.is_foreign else "",
            num(s.total_foreign) if money and s.is_foreign else "",
            num(s.total_cost) if money else "", num(returned) if money else "",
            num(s.paid_total) if money else "", num(s.debt) if money else "",
            num(s.overpaid) if money else "", num(s.stated_total) if money else "",
            "да" if s.is_opening else "", str(dup) if dup else "", _text(s.note),
            s.created_by.username if s.created_by_id else "",
        ]
