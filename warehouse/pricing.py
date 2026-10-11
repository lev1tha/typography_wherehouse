"""Цена продажи против закупа (STK-03, волна 2).

Цена в карточке вводится руками и от партий не зависела вовсе: пришла партия
дороже — карточка продаёт по-старому, и лист уходил с маржой −1 400 без единого
предупреждения. Здесь — закуп последней партии в единицах продажи, цена-
подсказка по наценке материала и маржа текущей цены. Ничего не пишет: цену
меняет человек.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

CENT = Decimal("0.01")


def last_lot(material):
    """Самая свежая партия с ценой (по дате поступления). Нет — None."""
    lots = [r for r in material.rolls.all() if r.purchase_cost and r.initial_area]
    if not lots:
        return None
    return max(lots, key=lambda r: (r.received_at, r.pk))


def main_unit(material) -> str:
    """В чём материал продаётся прежде всего: pm (рулон), sheet (лист с ценой
    листа), sqm (площадной и лист без цены листа), unit (штучный).

    Лист с ценой листа — «sheet» (S3, STK-03): владелец смотрит маржу листа по
    цене листа, (7 000 − 3 480) / 7 000 = 50,3 %; маржа цены кв.м (53,2 %)
    для листа — другая цифра, и в каталоге она путала."""
    if material.sells_by_metre:
        return "pm"
    if not material.is_roll_material:
        return "unit"
    if (
        material.intake_form == material.IntakeForm.SHEET
        and material.piece_price and material.piece_area and material.piece_area > 0
    ):
        return "sheet"
    return "sqm"


def unit_costs(material) -> dict:
    """Закуп последней партии: {sqm, sheet, pm, unit} — что применимо.

    Без партий — последняя закупочная из карточки (её же берёт оценка склада).
    """
    lot = last_lot(material)
    per = lot.cost_of(Decimal("1")) if lot is not None else (material.purchase_price or Decimal("0"))
    if not per:
        return {}
    if material.sells_by_metre:
        width = (lot.width if lot is not None and lot.width else None) or material.roll_width
        out = {"sqm": per}
        if width:
            out["pm"] = per * Decimal(width)
        return out
    if material.is_roll_material:
        out = {"sqm": per}
        if material.piece_area and material.piece_area > 0:
            out["sheet"] = per * material.piece_area
        return out
    return {"unit": per}


def prices(material) -> dict:
    return {
        "sqm": material.sqm_price,
        "sheet": material.piece_price,
        "pm": material.price_per_pm,
        "unit": material.price_per_unit,
    }


def suggest(cost: Decimal, markup) -> Decimal:
    """Закуп × (1 + наценка), вверх до целого сома: подсказка не должна
    оказаться ниже заданной наценки из-за копеек."""
    value = Decimal(cost) * (Decimal("1") + Decimal(markup) / Decimal("100"))
    return value.quantize(Decimal("1"), rounding=ROUND_CEILING)


def pricing_hint(material) -> dict:
    """Подсказка для карточки и каталога.

    cost — закуп последней партии по единицам; suggested — цена по наценке
    (пусто, если наценка не задана); margin_percent — маржа основной цены
    (доля цены, %); below_cost — какие цены ниже закупа (sqm, sheet, pm,
    unit, wholesale).
    """
    # Закуп в единице — до тыйына, как его видит владелец (3 480,00 за лист), и
    # от него же подсказка и маржа (S3, STK-03): от сырого 1 169,0406… × 2,9768
    # = 3 480,0000…1 подсказка вверх до сома давала 5 221 вместо 5 220.
    costs = {k: c.quantize(CENT) for k, c in unit_costs(material).items()}
    price = prices(material)
    unit = main_unit(material)
    below = [k for k, c in costs.items() if price.get(k) and price[k] < c]
    if material.wholesale_price and "sheet" in costs and material.wholesale_price < costs["sheet"]:
        below.append("wholesale")
    main_cost, main_price = costs.get(unit), price.get(unit)
    margin = None
    if main_cost and main_price:
        margin = ((main_price - main_cost) / main_price * 100).quantize(Decimal("0.1"))
    markup = material.markup_percent
    suggested = {k: suggest(c, markup) for k, c in costs.items()} if markup is not None else {}
    return {
        "unit": unit,
        "cost": costs,
        "suggested": suggested,
        "margin_percent": margin,
        "below_cost": below,
    }
