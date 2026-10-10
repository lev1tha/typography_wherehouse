"""Минимальный остаток в единицах материала и «К заказу» (STK-06, волна 2).

Порог вводился в кв.м: «5 листов» владелец пересчитывал в 14,88 сам, а
Telegram писал «Осталось всего 11.9072000» без единицы. Здесь — единица
материала (лист, метр, штука), перевод в единицу хранения и таблица «что и
сколько докупить»: до двух минимумов, у поставщика последней партии.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from .models import Material


def unit_kind(material: Material) -> str:
    """sheet — лист, pm — погонный метр рулона, sqm — площадной без листа,
    unit — штучный (шт, кг, л)."""
    if material.sells_by_metre:
        return "pm"
    if material.is_roll_material:
        return "sheet" if material.piece_area and material.piece_area > 0 else "sqm"
    return "unit"


def unit_label(material: Material) -> str:
    kind = unit_kind(material)
    return {"sheet": "лист.", "pm": "м", "sqm": "кв.м"}.get(kind) or material.get_unit_display()


def to_stock_units(material: Material, units) -> Decimal:
    """Количество в единицах материала → в единицах хранения (кв.м у листа и
    рулона), вверх до сотых: порог «5 листов» срабатывает на пятом листе."""
    units = Decimal(str(units))
    kind = unit_kind(material)
    if kind == "sheet":
        value = units * material.piece_area
    elif kind == "pm" and material.roll_width:
        value = units * Decimal(material.roll_width)
    else:
        value = units
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def stock_in_units(material: Material) -> Decimal:
    """Остаток в единицах материала: листы, метры (по ширине каждого рулона),
    кв.м или штуки."""
    kind = unit_kind(material)
    if kind == "sheet":
        return (material.quantity / material.piece_area).quantize(Decimal("0.01"))
    if kind == "pm":
        metres = material.metres_remaining
        return metres if metres is not None else Decimal("0")
    return (material.quantity or Decimal("0")).quantize(Decimal("0.01"))


def min_in_units(material: Material) -> Decimal:
    """Минимум в единицах материала: заданный — как есть, иначе из порога кв.м."""
    if material.min_stock is not None:
        return material.min_stock
    crit = material.critical_balance or Decimal("0")
    kind = unit_kind(material)
    if kind == "sheet":
        return (crit / material.piece_area).quantize(Decimal("0.01"))
    if kind == "pm" and material.roll_width:
        return (crit / Decimal(material.roll_width)).quantize(Decimal("0.01"))
    return crit


def _round_order(material: Material, value: Decimal) -> Decimal:
    """Листы и штуки — целыми вверх; метры — целыми метрами вверх; кв.м — сотые."""
    if value <= 0:
        return Decimal("0")
    if unit_kind(material) == "sqm":
        return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return value.quantize(Decimal("1"), rounding=ROUND_CEILING)


def last_supplier(material: Material):
    """Поставщик последней партии, пришедшей накладной (название), или None."""
    from .pricing import last_lot

    lot = last_lot(material)
    if lot is None:
        return None
    from .models import SupplyLine

    try:
        supply = lot.supply_line.supply
    except SupplyLine.DoesNotExist:  # партия без накладной (одиночный приход)
        return None
    return supply.supplier.name if supply.supplier_id else None


def reorder_rows(queryset=None) -> list[dict]:
    """«К заказу»: материалы с порогом, упавшие до него.

    Докупить до двух минимумов (правило Excel владельца: 2 × мин − остаток),
    по закупу последней партии — ориентир суммы.
    """
    from .pricing import unit_costs

    qs = queryset if queryset is not None else Material.objects.filter(is_archived=False)
    out = []
    for m in qs.prefetch_related("rolls__supply_line__supply__supplier"):
        minimum = min_in_units(m)
        if not minimum or minimum <= 0:
            continue
        stock = stock_in_units(m)
        if stock > minimum:
            continue
        kind = unit_kind(m)
        need = _round_order(m, minimum * 2 - stock)
        cost = unit_costs(m).get(kind)
        out.append({
            "id": m.pk,
            "name": m.name,
            "unit": kind,
            "unit_label": unit_label(m),
            "stock": stock,
            "min": minimum,
            "to_order": need,
            "supplier": last_supplier(m),
            "unit_cost": cost.quantize(Decimal("0.01")) if cost else None,
            "sum": (need * cost).quantize(Decimal("0.01")) if cost else None,
        })
    out.sort(key=lambda r: r["name"])
    return out


def low_stock_text(material: Material) -> str:
    """Текст оповещения: с единицей, без хвоста нулей, сколько докупить."""
    def n(value):
        return format(Decimal(value).normalize(), "f").replace(".", ",")

    unit = unit_label(material)
    stock = stock_in_units(material)
    minimum = min_in_units(material)
    text = (
        f"⚠️ <b>Внимание!</b> Материал «{material.name}» на исходе: "
        f"осталось {n(stock)} {unit}"
    )
    if unit_kind(material) in ("sheet", "pm"):
        text += f" ({n(material.quantity.quantize(Decimal('0.01')))} кв.м)"
    if minimum and minimum > 0:
        need = _round_order(material, minimum * 2 - stock)
        text += f", минимум {n(minimum)} {unit}. Заказать ≈{n(need)} {unit}"
        supplier = last_supplier(material)
        if supplier:
            text += f" (последний поставщик: {supplier})"
        text += "."
    else:
        text += ". Требуется закупка!"
    return text
