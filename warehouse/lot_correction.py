"""«Исправить приход» — опечатка в цене или количестве принятой партии.

Аудит 10.10 (XL-02, STK-02, PNL-02, F2): в Excel опечатка в закупке — правка
ячейки. В системе после первой продажи её было не исправить: команда
`fix_lot_cost` двигала цену партии, но не себестоимость уже проданного, а
количество не правилось вовсе. Обход — фиктивное списание — уводил прибыль.

Что делает исправление (одной транзакцией, только администратор):

* ПАРТИЯ: размеры/количество, сумма закупки; остаток партии и материала
  меняется на разницу. Меньше, чем из партии уже ушло, сделать нельзя.
* ПРОДАННОЕ: себестоимость строк чеков, бравших из партии
  (`TransactionItemLot`), пересчитывается по новой цене пропорционально
  взятому. Строки до 10.10 записей о партиях не имеют — их себестоимость
  восстанавливается, только если она до тыйына равна «площадь × цена этой
  партии», строка помнит именно эту партию и у материала нет другой партии с
  той же ценой (`_sold_lines`); остальные не трогаются и перечисляются.
* НАКЛАДНАЯ: строка (количество, сумма) — итог и долг поставщику считаются из
  неё как и раньше; «оплачено» не трогаем. У одиночной партии долг
  (`Roll.supplier_debt`) = новая сумма − уже заплаченное по кассе.
* ЖУРНАЛ СКЛАДА: запись прихода правится на месте (по ней считаются закуп
  одиночных приходов, складской лист и склад на дату — второй формулы не
  нужно), рядом пишется запись «Исправление прихода» с «было → стало».
  Количество в ней 0 и себестоимости нет: это не продажа, не потеря и не
  промер, в ОПиУ она не попадает.
* ЗАМОК ПЕРИОДА: меняется закуп месяца прихода и себестоимость месяцев продаж —
  если хоть один закрыт, отказ с перечнем месяцев.

Чего НЕ делает: списания и промеры из партии (брак, недостача) остаются по
старой цене — у записи журнала нет ссылки на партию; предпросмотр об этом
говорит («untracked_usage»).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from datetime import datetime, time

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import InventoryLog, Material, Roll, SupplyLine
from .rolls import compute_area

CENT = Decimal("0.01")
AREA = Decimal("0.0001")
TINY = Decimal("0.0001")


class CorrectionError(Exception):
    """Исправить нельзя — с человеческим объяснением."""

    def __init__(self, message, *, closed_months=None):
        super().__init__(message)
        self.closed_months = closed_months or []


def _money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _som(value) -> str:
    """12000 → «12 000», 12000.5 → «12 000.50»: в журнале — как на бумаге."""
    v = _money(value)
    whole = v == v.to_integral_value()
    text = f"{int(v):,}" if whole else f"{v:,.2f}"
    return text.replace(",", " ")


def _num(value) -> str:
    if value is None:
        return "—"
    return format(Decimal(value).normalize(), "f")


def _s(value):
    """Decimal → строка для ответа (DRF отдал бы float и потерял тыйыны)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _s(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_s(v) for v in value]
    return value


def _month(day) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_label(key: str) -> str:
    year, month = key.split("-")
    return f"{month}.{year}"


def _local(value):
    return timezone.localtime(value).date() if value else None


def sheet_size_warning(material, width, height):
    """Размер листа в приходе не совпадает с карточкой (аудит F7).

    Лист продаётся ЦЕЛИКОМ по площади листа из карточки: принятая пачка
    1.22×2.44 под карточкой 1×2 после продажи всех листов оставляет на складе
    «полчаса листа», которых на полке нет. Не запрет — бывает, что под одной
    карточкой законно другой формат, — но сказать надо в момент приёмки.
    """
    if not (width and height and material.sheet_width and material.sheet_height):
        return None
    got = sorted([Decimal(width), Decimal(height)])
    card = sorted([material.sheet_width, material.sheet_height])
    if all(abs(a - b) <= Decimal("0.005") for a, b in zip(got, card)):
        return None
    return {
        "code": "sheet_size_mismatch",
        "material": material.id,
        "message": (
            f"«{material.name}»: размер листа {_num(width)}×{_num(height)} не совпадает "
            f"с карточкой ({_num(material.sheet_width)}×{_num(material.sheet_height)}). "
            "Лист продаётся по площади из карточки — проверьте размер, иначе на складе "
            "останется часть листа, которой нет на полке."
        ),
    }


# --- Запись прихода в журнале -------------------------------------------------


def _pick(candidates, twins: int):
    """Из записей-кандидатов взять одну, если это не догадка.

    Одинаковые записи (то же количество, цена, дата, накладная) у одинаковых
    партий взаимозаменяемы: их ровно столько, сколько таких партий, и какая
    чьей станет — неважно. Если записей больше, чем партий, одна из них лишняя
    (двойной ввод), и правка «не той» развела бы закуп со складом — отказ.
    """
    if len(candidates) == 1:
        return candidates[0]
    if candidates and len(candidates) <= twins:
        key = {(c.quantity_changed, c.actual_price, c.happened_at, c.supply_id) for c in candidates}
        if len(key) == 1:
            return candidates[0]
    return None


def supply_log_for_roll(roll: Roll):
    """Запись «Поступление» этой партии — или None, если однозначно не найти."""
    own = list(InventoryLog.objects.filter(roll=roll, type=InventoryLog.Type.SUPPLY))
    if own:
        return own[0] if len(own) == 1 else None
    qs = InventoryLog.objects.filter(
        material_id=roll.material_id, type=InventoryLog.Type.SUPPLY,
        quantity_changed=roll.initial_area, roll__isnull=True,
    )
    try:
        line = roll.supply_line
    except SupplyLine.DoesNotExist:
        line = None
    if line is not None:
        qs = qs.filter(supply_id=line.supply_id)
    candidates = list(qs.order_by("id"))
    if len(candidates) > 1:
        same = [c for c in candidates if c.happened_at == roll.received_at]
        if same:
            candidates = same
    twins = Roll.objects.filter(
        material_id=roll.material_id, initial_area=roll.initial_area,
        received_at=roll.received_at,
    ).exclude(inventory_logs__type=InventoryLog.Type.SUPPLY).count()
    return _pick(candidates, twins)


def _supply_log_for_line(line: SupplyLine):
    candidates = list(InventoryLog.objects.filter(
        supply_id=line.supply_id, material_id=line.material_id,
        type=InventoryLog.Type.SUPPLY, quantity_changed=line.quantity, roll__isnull=True,
    ).order_by("id"))
    twins = SupplyLine.objects.filter(
        supply_id=line.supply_id, material_id=line.material_id,
        quantity=line.quantity, roll__isnull=True,
    ).count()
    return _pick(candidates, twins)


# --- Новые размеры ------------------------------------------------------------


def _given(data, key):
    value = data.get(key)
    return None if value in (None, "") else Decimal(str(value))


def _unit_label(roll: Roll | None, material: Material) -> str:
    if roll is None or roll.form == Roll.Form.PIECE:
        return material.get_unit_display()
    if roll.form == Roll.Form.ROLL and roll.width:
        return "м"
    if roll.form == Roll.Form.SHEET and roll.width and roll.height:
        return "лист(ов)"
    return "кв.м"


def _in_units(roll: Roll | None, area: Decimal, width=None, height=None) -> Decimal:
    """Площадь партии в её «человеческой» единице: метры, листы, штуки."""
    if roll is not None and roll.form == Roll.Form.ROLL and (width or roll.width):
        return (area / (width or roll.width)).quantize(CENT)
    if roll is not None and roll.form == Roll.Form.SHEET and (width or roll.width) and (height or roll.height):
        return (area / ((width or roll.width) * (height or roll.height))).quantize(CENT)
    return area.quantize(CENT) if area == area.quantize(CENT) else area


def _new_lot_shape(roll: Roll, data, used: Decimal) -> dict:
    """Размеры партии после правки и её площадь (кв.м или штуки)."""
    width = _given(data, "width") or roll.width
    height = _given(data, "height") or roll.height
    length = _given(data, "length") or roll.length
    count = _given(data, "sheet_count") or roll.sheet_count
    quantity = _given(data, "quantity")
    for name in ("width", "height", "length", "sheet_count", "quantity"):
        value = _given(data, name)
        if value is not None and value <= 0:
            raise CorrectionError("Размеры и количество должны быть больше нуля.")

    if roll.form == Roll.Form.PIECE:
        count = quantity or _given(data, "sheet_count") or roll.initial_area
        return {"width": None, "height": None, "length": None, "sheet_count": count,
                "area": compute_area(Roll.Form.PIECE, sheet_count=count)}
    if roll.form == Roll.Form.SHEET:
        if width and height and count:
            return {"width": width, "height": height, "length": roll.length, "sheet_count": count,
                    "area": compute_area(Roll.Form.SHEET, width=width, height=height, sheet_count=count)}
        return {"width": roll.width, "height": roll.height, "length": roll.length,
                "sheet_count": roll.sheet_count, "area": (quantity or roll.initial_area)}
    # Рулон. Ширину после резки не правим: метры уже проданы по ней, и
    # площадь каждой проданной строки посчитана этой шириной.
    if width != roll.width and used > 0:
        raise CorrectionError(
            "Ширину рулона после продаж и списаний не правим: проданные метры "
            "посчитаны по ней. Исправьте длину или сумму."
        )
    if width and length:
        return {"width": width, "height": roll.height, "length": length,
                "sheet_count": roll.sheet_count,
                "area": compute_area(Roll.Form.ROLL, width=width, length=length)}
    return {"width": roll.width, "height": roll.height, "length": roll.length,
            "sheet_count": roll.sheet_count, "area": (quantity or roll.initial_area)}


# --- Проданное ----------------------------------------------------------------


def _legacy_area(item, roll: Roll | None) -> Decimal:
    """Сколько склада списала строка «до 10.10» — тем же правилом, что
    `_move_stock_for_item`: метры × ширина партии, листы × площадь листа."""
    from sales.models import TransactionItem

    if item.sale_mode == TransactionItem.SaleMode.METER:
        return item.quantity * (roll.width if roll is not None and roll.width else Decimal("0"))
    if item.sale_mode == TransactionItem.SaleMode.PIECE and item.material.piece_area:
        return item.quantity * item.material.piece_area
    return item.quantity


def _sold_lines(roll: Roll | None, material: Material, since, cps0, cps1, used):
    """Строки чеков, чья себестоимость меняется, и старые, которые не тронем.

    Возвращает (changes, legacy, recorded_area, inferred_area):
    changes — [{item, area, delta, how}], legacy — [item].
    """
    from sales.models import Receipt, TransactionItem, TransactionItemLot

    changes, legacy = [], []
    recorded_area = inferred_area = Decimal("0")
    if roll is not None:
        per_item = defaultdict(lambda: Decimal("0"))
        for use in TransactionItemLot.objects.filter(roll=roll, item__is_returned=False):
            per_item[use.item_id] += use.area
        recorded_area = sum(per_item.values(), Decimal("0"))
        items = {
            it.id: it for it in TransactionItem.objects.filter(pk__in=list(per_item))
            .select_related("receipt", "material")
        }
        for item_id, area in per_item.items():
            delta = _money(area * cps1) - _money(area * cps0)
            if delta:
                changes.append({"item": items[item_id], "area": area, "delta": delta, "how": "lots"})

    if cps1 == cps0:
        return changes, legacy, recorded_area, inferred_area

    # Строки до учёта партий по строке: записей нет. Кандидаты — проданные
    # после прихода партии или помнящие её (`TransactionItem.roll`).
    old = (
        TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL, material=material, is_returned=False,
            lot_uses__isnull=True,
        )
        .exclude(receipt__status=Receipt.Status.CANCELLED)
        .select_related("receipt", "material")
        .order_by("receipt__created_at", "id")
    )
    window = Q(receipt__created_at__gte=since)
    if roll is not None:
        window |= Q(roll=roll)
    old = list(old.filter(window).distinct())
    if roll is None:
        return changes, old, recorded_area, inferred_area

    twin_price = Roll.objects.filter(material=material).exclude(pk=roll.pk).filter(
        purchase_cost__gt=0,
    )
    same_price = any(r.cost_per_sqm == cps0 for r in twin_price)
    inferred = []
    for item in old:
        area = _legacy_area(item, roll)
        if (
            item.roll_id == roll.id and area > 0 and not same_price
            and abs(item.cost_total - _money(area * cps0)) <= CENT
        ):
            inferred.append((item, area))
        else:
            legacy.append(item)
    inferred_area = sum((a for _, a in inferred), Decimal("0"))
    # Вместе со строками по записям не больше, чем из партии ушло: иначе
    # какая-то из строк брала и из других партий — догадка, а не факт.
    if recorded_area + inferred_area > used + TINY:
        legacy = [it for it, _ in inferred] + legacy
        inferred, inferred_area = [], Decimal("0")
    for item, area in inferred:
        delta = _money(area * cps1) - _money(area * cps0)
        if delta:
            changes.append({"item": item, "area": area, "delta": delta, "how": "inferred"})
    return changes, legacy, recorded_area, inferred_area


# --- План и применение --------------------------------------------------------


def _stock_value_after(material: Material, roll: Roll | None, *, area1, rem1, cost1,
                       qty_delta, purchase_price) -> Decimal:
    m = Material.objects.prefetch_related("rolls").get(pk=material.pk)
    for r in m.rolls.all():
        if roll is not None and r.pk == roll.pk:
            r.initial_area, r.remaining_area, r.purchase_cost = area1, rem1, cost1
    m.quantity = (m.quantity or Decimal("0")) + qty_delta
    m.purchase_price = purchase_price
    return m.stock_value


def _plan(*, roll: Roll | None, line: SupplyLine | None, data) -> dict:
    from finance.periods import is_closed

    material = Material.objects.get(pk=(roll or line).material_id)
    supply = line.supply if line is not None else None
    warnings = []

    if roll is not None:
        area0, rem0, cost0 = roll.initial_area, roll.remaining_area, roll.purchase_cost
        used = area0 - rem0
        shape = _new_lot_shape(roll, data, used)
        area1 = Decimal(shape["area"]).quantize(AREA)
        cps0 = roll.cost_per_sqm
        since = roll.received_at
        purchase_day = supply.received_on if supply else _local(roll.received_at)
    else:
        area0 = line.quantity
        cost0 = line.cost
        used = Decimal("0")
        quantity = _given(data, "quantity")
        if quantity is not None and quantity <= 0:
            raise CorrectionError("Количество должно быть больше нуля.")
        shape = {"width": None, "height": None, "length": None, "sheet_count": None,
                 "area": quantity or area0}
        area1 = Decimal(shape["area"])
        cps0 = (cost0 / area0).quantize(CENT) if area0 else Decimal("0")
        since = None
        purchase_day = supply.received_on

    cost = _given(data, "purchase_cost")
    if cost is not None and cost < 0:
        raise CorrectionError("Сумма закупки не может быть отрицательной.")
    if cost is None:
        # Цену не трогали — цена ЕДИНИЦЫ та же, сумма идёт за количеством:
        # так считает и бумажная накладная (листов больше — сумма больше).
        cost1 = _money(cost0 * area1 / area0) if area0 and area1 != area0 else cost0
    else:
        cost1 = _money(cost)
    cps1 = (cost1 / area1).quantize(CENT) if area1 else Decimal("0")
    qty_delta = area1 - area0

    unit = _unit_label(roll, material)
    if roll is not None and area1 + TINY < used:
        gone = _in_units(roll, used)
        raise CorrectionError(
            f"Из партии уже ушло {_num(gone)} {unit} — продажами или списанием; "
            f"меньше нельзя (введено {_num(_in_units(roll, area1, shape['width'], shape['height']))} {unit})"
        )
    if roll is None and qty_delta < 0:
        in_lots = sum((r.remaining_area for r in material.rolls.all()), Decimal("0"))
        loose = (material.quantity or Decimal("0")) - in_lots
        if loose + qty_delta < -TINY:
            raise CorrectionError(
                f"«{material.name}»: на складе осталось {_num(max(loose, Decimal('0')))} "
                f"{unit} — часть уже продана или списана, убрать {_num(-qty_delta)} {unit} нельзя."
            )

    dims_changed = roll is not None and any(
        (shape[k] or None) != (getattr(roll, k) or None)
        for k in ("width", "height", "length", "sheet_count")
    )
    if area1 == area0 and cost1 == cost0 and not dims_changed:
        raise CorrectionError("Ничего не меняется: введите другие цену, количество или размеры.")

    if roll is not None and roll.form == Roll.Form.SHEET:
        w = sheet_size_warning(material, shape["width"], shape["height"])
        if w:
            warnings.append(w)

    # Проданное.
    if since is None:
        since = timezone.make_aware(datetime.combine(purchase_day, time.min))
    changes, legacy, recorded_area, inferred_area = _sold_lines(
        roll, material, since, cps0, cps1, used,
    )
    if roll is not None and cps1 != cps0:
        untracked = used - recorded_area - inferred_area - sum(
            (_legacy_area(it, roll) for it in legacy if it.roll_id == roll.id), Decimal("0"))
        if untracked > TINY:
            warnings.append({
                "code": "untracked_usage",
                "message": (
                    f"Из партии ушло ещё {_num(_in_units(roll, untracked))} {unit} мимо продаж с "
                    "записью партии (брак, промер, старые продажи) — их себестоимость остаётся прежней."
                ),
            })

    # Чеки.
    by_receipt = {}
    for ch in changes:
        item = ch["item"]
        rec = by_receipt.setdefault(item.receipt_id, {"receipt": item.receipt, "delta": Decimal("0"), "items": []})
        rec["delta"] += ch["delta"]
        rec["items"].append({
            "id": item.id, "how": ch["how"], "area": ch["area"],
            "cost_before": item.cost_total, "cost_after": item.cost_total + ch["delta"],
        })
    receipts, days = [], set()
    if cost1 != cost0 or qty_delta:
        days.add(purchase_day)
    for rec in by_receipt.values():
        receipt = rec["receipt"]
        day = _local(receipt.revenue_recognized_at)
        if day:
            days.add(day)
        cost_before = receipt.cost_total
        margin_before = receipt.margin
        receipts.append({
            "id": str(receipt.id), "order_number": receipt.order_number,
            "date": day.isoformat() if day else None,
            "cost_before": cost_before, "cost_after": cost_before + rec["delta"],
            "margin_before": margin_before, "margin_after": margin_before - rec["delta"],
            "items": rec["items"],
        })
    receipts.sort(key=lambda r: (r["date"] or "", r["order_number"] or 0))
    months = sorted({_month(d) for d in days})
    closed = sorted({_month(d) for d in days if is_closed(d)})

    # Склад.
    latest_roll = material.rolls.order_by("-received_at", "-id").first()
    purchase_price = material.purchase_price
    supply_log = supply_log_for_roll(roll) if roll is not None else _supply_log_for_line(line)
    if roll is not None and latest_roll is not None and latest_roll.pk == roll.pk:
        purchase_price = cps1
    elif roll is None and supply_log is not None:
        newer = InventoryLog.objects.filter(
            material=material, type=InventoryLog.Type.SUPPLY, happened_at__gt=supply_log.happened_at,
        ).exists() or (latest_roll is not None and latest_roll.received_at > supply_log.happened_at)
        if not newer:
            purchase_price = cps1
    if supply_log is None:
        warnings.append({
            "code": "journal_ambiguous",
            "message": (
                "Запись прихода в журнале склада однозначно не найдена (двойной ввод?) — "
                "применить исправление нельзя, иначе закуп разойдётся со складом."
            ),
        })
    rem1 = (rem0 + qty_delta) if roll is not None else None
    value_before = material.stock_value
    value_after = _stock_value_after(
        material, roll, area1=area1, rem1=rem1, cost1=cost1,
        qty_delta=qty_delta, purchase_price=purchase_price,
    )

    plan = {
        "target": {
            "roll": roll.id if roll is not None else None,
            "supply_line": line.id if line is not None else None,
            "supply": supply.id if supply is not None else None,
            "supply_number": supply.number if supply is not None else None,
            "material": material.id, "material_name": material.name,
            "form": roll.form if roll is not None else "QTY",
            "unit": unit,
            "label": roll.dimensions_label if roll is not None else f"{_num(area0)} {unit}",
        },
        "before": {
            "width": roll.width if roll else None, "height": roll.height if roll else None,
            "length": roll.length if roll else None, "sheet_count": roll.sheet_count if roll else None,
            "quantity": area0, "units": _in_units(roll, area0) if roll else area0,
            "remaining": rem0 if roll is not None else None,
            "purchase_cost": cost0, "cost_per_sqm": cps0,
        },
        "after": {
            "width": shape["width"], "height": shape["height"], "length": shape["length"],
            "sheet_count": shape["sheet_count"], "quantity": area1,
            "units": _in_units(roll, area1, shape["width"], shape["height"]) if roll else area1,
            "remaining": rem1, "purchase_cost": cost1, "cost_per_sqm": cps1,
        },
        "used": used,
        "stock": {
            "quantity_before": material.quantity,
            "quantity_after": (material.quantity or Decimal("0")) + qty_delta,
            "value_before": value_before, "value_after": value_after,
            "value_delta": value_after - value_before,
            "purchase_price_before": material.purchase_price,
            "purchase_price_after": purchase_price,
        },
        "supply": None, "lot_debt": None,
        "receipts": receipts,
        "cogs_delta": sum((c["delta"] for c in changes), Decimal("0")),
        "inferred_count": sum(1 for c in changes if c["how"] == "inferred"),
        "legacy_count": len(legacy),
        "legacy": [
            {"item": it.id, "receipt": str(it.receipt_id), "order_number": it.receipt.order_number,
             "date": (_local(it.receipt.revenue_recognized_at) or _local(it.receipt.created_at)).isoformat(),
             "cost": it.cost_total}
            for it in legacy
        ],
        "months": months,
        "closed_months": closed,
        "warnings": warnings,
        # Для применения — не для ответа.
        "_changes": changes, "_shape": shape, "_supply_log": supply_log,
        "_material": material,
    }
    if supply is not None:
        total_before = supply.total_cost
        total_after = total_before - line.cost + cost1
        paid = supply.paid_amount or Decimal("0")
        plan["supply"] = {
            "id": supply.id, "number": supply.number,
            "total_before": total_before, "total_after": total_after,
            "paid": paid,
            "debt_before": max(total_before - paid, Decimal("0")),
            "debt_after": max(total_after - paid, Decimal("0")),
            "overpaid_before": max(paid - total_before, Decimal("0")),
            "overpaid_after": max(paid - total_after, Decimal("0")),
            "stated_total": supply.stated_total,
            "discrepancy_before": (supply.stated_total - total_before) if supply.stated_total is not None else None,
            "discrepancy_after": (supply.stated_total - total_after) if supply.stated_total is not None else None,
        }
    elif roll is not None:
        paid = _lot_paid(roll)
        tracked = roll.supplier_debt > 0 or paid != 0
        plan["lot_debt"] = {
            "tracked": tracked, "paid": paid,
            "debt_before": roll.supplier_debt,
            "debt_after": max(cost1 - paid, Decimal("0")) if tracked else roll.supplier_debt,
            "overpaid_after": max(paid - cost1, Decimal("0")) if tracked else Decimal("0"),
        }
    return plan


def _lot_paid(roll: Roll) -> Decimal:
    """Сколько уже заплачено поставщику за одиночную партию — по кассе."""
    from finance.models import CashEntry

    paid = Decimal("0")
    for kind, amount in CashEntry.objects.filter(
        roll=roll, article=CashEntry.Article.SUPPLY
    ).values_list("kind", "amount"):
        paid += amount if kind == CashEntry.Kind.OUT else -amount
    return paid


def public(plan: dict) -> dict:
    return _s({k: v for k, v in plan.items() if not k.startswith("_")})


def preview(*, roll=None, line=None, data) -> dict:
    with transaction.atomic():
        if roll is None and line is not None and line.roll_id:
            roll = line.roll
        if roll is not None and line is None:
            line = SupplyLine.objects.filter(roll=roll).select_related("supply").first()
        return public(_plan(roll=roll, line=line, data=data))


@transaction.atomic
def apply(*, roll=None, line=None, data, user=None) -> dict:
    # Замки: партия, строка, накладная, материал — до расчёта, чтобы план
    # считался по тем же числам, которые будем менять.
    if roll is None and line is not None and line.roll_id:
        roll = line.roll
    if roll is not None:
        roll = Roll.objects.select_for_update().get(pk=roll.pk)
        line = SupplyLine.objects.select_for_update().filter(roll=roll).first()
    else:
        line = SupplyLine.objects.select_for_update().get(pk=line.pk)
    if line is not None:
        from .models import Supply

        Supply.objects.select_for_update().get(pk=line.supply_id)
        line = SupplyLine.objects.select_related("supply").get(pk=line.pk)
    Material.objects.select_for_update().get(pk=(roll or line).material_id)

    plan = _plan(roll=roll, line=line, data=data)
    if plan["closed_months"]:
        months = ", ".join(_month_label(m) for m in plan["closed_months"])
        raise CorrectionError(
            f"Исправление меняет закуп или себестоимость закрытых месяцев: {months}. "
            "Откройте период в Финансах или оставьте как есть.",
            closed_months=plan["closed_months"],
        )
    log = plan["_supply_log"]
    if log is None:
        raise CorrectionError(
            "Не нашлась однозначная запись прихода в журнале склада (двойной ввод?) — "
            "исправить автоматически нельзя, иначе закуп разойдётся со складом."
        )

    from sales.models import TransactionItem

    material = Material.objects.get(pk=plan["_material"].pk)
    shape = plan["_shape"]
    before, after = plan["before"], plan["after"]
    qty_delta = after["quantity"] - before["quantity"]
    today = timezone.localdate()

    # Строки чеков — под замком, себестоимость на разницу.
    ids = [c["item"].id for c in plan["_changes"]]
    locked = {it.id: it for it in TransactionItem.objects.select_for_update().filter(pk__in=ids)}
    for ch in plan["_changes"]:
        item = locked[ch["item"].id]
        item.cost_total = _money(item.cost_total + ch["delta"])
        item.save(update_fields=["cost_total"])

    if roll is not None:
        roll.initial_area = after["quantity"]
        roll.remaining_area = roll.remaining_area + qty_delta
        roll.purchase_cost = after["purchase_cost"]
        for k in ("width", "height", "length", "sheet_count"):
            setattr(roll, k, shape[k])
        fields = ["initial_area", "remaining_area", "purchase_cost", "width", "height",
                  "length", "sheet_count"]
        if plan["lot_debt"] and plan["lot_debt"]["tracked"]:
            roll.supplier_debt = plan["lot_debt"]["debt_after"]
            fields.append("supplier_debt")
        roll.save(update_fields=fields)
    if line is not None:
        line.quantity = after["quantity"]
        line.cost = after["purchase_cost"]
        if roll is not None:
            for k in ("width", "height", "length", "sheet_count"):
                setattr(line, k, shape[k])
        line.save(update_fields=["quantity", "cost", "width", "height", "length", "sheet_count"])

    material.quantity = (material.quantity or Decimal("0")) + qty_delta
    material.purchase_price = plan["stock"]["purchase_price_after"]
    material.save(update_fields=["quantity", "purchase_price", "updated_at"])

    # Приход в журнале — на месте, как ячейка в Excel.
    log.quantity_changed = after["quantity"]
    log.actual_price = after["cost_per_sqm"]
    if roll is not None:
        log.roll = roll
        if roll.form == Roll.Form.ROLL:
            log.metres_changed = roll.metres_initial
        log.reason = (
            f"Поступление: {roll.dimensions_label} "
            f"({after['quantity'].quantize(CENT)} кв.м), {_som(after['purchase_cost'])} сом "
            f"(исправлено {today:%d.%m.%Y})"
        )
    log.save(update_fields=["quantity_changed", "actual_price", "roll", "metres_changed", "reason"])

    unit = plan["target"]["unit"]
    changes_text = []
    if before["quantity"] != after["quantity"] or before["units"] != after["units"]:
        changes_text.append(f"количество {_num(before['units'])} → {_num(after['units'])} {unit}")
    for k, name in (("width", "ширина"), ("height", "высота"), ("length", "длина")):
        if before[k] != after[k] and after[k] is not None:
            changes_text.append(f"{name} {_num(before[k])} → {_num(after[k])}")
    if before["purchase_cost"] != after["purchase_cost"]:
        changes_text.append(
            f"сумма {_som(before['purchase_cost'])} → {_som(after['purchase_cost'])} сом"
        )
    head = (
        f"партия №{roll.id}" if roll is not None
        else f"строка накладной №{line.supply.number or line.supply_id}"
    )
    what = "; ".join(changes_text)
    InventoryLog.objects.create(
        type=InventoryLog.Type.CORRECTION,
        material=material,
        quantity_changed=Decimal("0"),
        actual_price=after["cost_per_sqm"],
        reason=f"Исправление прихода ({head}): {what}",
        roll=roll,
        supply=line.supply if line is not None else None,
        created_by=user,
    )

    from audit.models import AuditLog

    numbers = ", ".join(f"№{r['order_number']}" for r in plan["receipts"]) or "нет"
    text = (
        f"Исправлен приход «{material.name}» ({head}, {plan['target']['label']}): {what}. "
        f"Себестоимость продаж {plan['cogs_delta']:+} сом, чеки: {numbers}; "
        f"склад {_som(plan['stock']['value_before'])} → {_som(plan['stock']['value_after'])} сом"
    )
    if plan["supply"]:
        s = plan["supply"]
        text += (
            f"; накладная {_som(s['total_before'])} → {_som(s['total_after'])} сом, "
            f"долг {_som(s['debt_before'])} → {_som(s['debt_after'])}"
        )
    elif plan["lot_debt"] and plan["lot_debt"]["tracked"]:
        d = plan["lot_debt"]
        text += f"; долг поставщику {_som(d['debt_before'])} → {_som(d['debt_after'])}"
    if plan["legacy_count"]:
        text += f"; не пересчитаны старые продажи без партий: {plan['legacy_count']}"
    if data.get("note"):
        text += f". {str(data['note'])[:200]}"
    AuditLog.record(user, text)
    return public(plan)
