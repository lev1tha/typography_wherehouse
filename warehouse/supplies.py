"""Приходная накладная: проведение и отмена.

Документ проводится сразу при создании — строки уходят на склад теми же
примитивами, что и одиночный приход (``receive_lot`` для площадных материалов,
``apply_stock_change`` для штучных). Своей «параллельной» механики склада здесь
нет намеренно: закуп в финотчёте, складской журнал и FIFO считаются по
движениям, и любой второй путь записи рано или поздно с ними разойдётся.
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import InventoryLog, Roll, Supply, SupplyLine
from .rolls import compute_area, receive_lot
from .stock import apply_stock_change


class SupplyError(Exception):
    """Накладную нельзя провести или отменить — с человеческим объяснением."""


def _moment(day):
    """Дата накладной → момент времени: по нему идут и журнал, и FIFO.

    ПОЛДЕНЬ, а не полночь — та же причина, что у даты заказа: полночь в Бишкеке
    это вчерашний вечер по UTC, и достаточно одной настройки часового пояса
    мимо, чтобы поставка уехала в предыдущий день.
    """
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


def line_quantity(material, form, *, width=None, height=None, length=None,
                  sheet_count=None, quantity=None) -> Decimal:
    """Сколько единиц встанет на склад по строке.

    Площадные материалы считаются в кв.м из размеров, штучные — прямым
    количеством. Одна функция на сервере и на форме: расхождение между тем, что
    показал предпросмотр, и тем, что легло на склад, — худший вид ошибки.
    """
    if form == SupplyLine.Form.QTY or not material.is_roll_material:
        return Decimal(str(quantity or 0))
    # Не хватает размеров — считать нечего. Возвращаем ноль, а объяснение
    # («проверьте размеры») даёт вызывающий: он знает, о какой строке речь.
    need = (width, height, sheet_count) if form == SupplyLine.Form.SHEET else (width, length)
    if not all(need):
        return Decimal("0")
    return compute_area(
        Roll.Form.SHEET if form == SupplyLine.Form.SHEET else Roll.Form.ROLL,
        width=width, length=length, height=height, sheet_count=sheet_count,
    )


@transaction.atomic
def post_supply(supply: Supply, lines_data: list[dict], *, user=None) -> Supply:
    """Провести накладную: создать строки и поднять по ним склад."""
    if not lines_data:
        raise SupplyError("В накладной нет ни одной строки.")

    happened_at = _moment(supply.received_on)
    reason_head = f"Накладная {supply.number}" if supply.number else "Приходная накладная"
    if supply.supplier_id:
        reason_head += f" · {supply.supplier.name}"

    for data in lines_data:
        material = data["material"]
        form = data.get("form") or (
            SupplyLine.Form.SHEET if material.is_roll_material else SupplyLine.Form.QTY
        )
        cost = Decimal(str(data.get("cost") or 0))
        # Лист без размеров — берём размер листа с материала (форма его так и
        # подставляет; здесь то же для тех, кто зовёт API напрямую).
        if form == SupplyLine.Form.SHEET and material.is_roll_material:
            if not data.get("width") and material.sheet_width:
                data["width"] = material.sheet_width
            if not data.get("height") and material.sheet_height:
                data["height"] = material.sheet_height
        qty = line_quantity(
            material, form,
            width=data.get("width"), height=data.get("height"),
            length=data.get("length"), sheet_count=data.get("sheet_count"),
            quantity=data.get("quantity"),
        )
        if qty <= 0:
            raise SupplyError(
                f"«{material.name}»: не из чего посчитать количество. "
                "Проверьте размеры или количество в строке."
            )

        line = SupplyLine(
            supply=supply, material=material, form=form,
            width=data.get("width"), height=data.get("height"),
            length=data.get("length"), sheet_count=data.get("sheet_count"),
            quantity=qty, cost=cost, code=data.get("code", "") or "",
        )

        if material.is_roll_material and form != SupplyLine.Form.QTY:
            # Площадный материал приходит партией: у неё своя себестоимость, по
            # ней потом считаются FIFO и стоимость склада.
            line.roll = receive_lot(
                material,
                form=Roll.Form.SHEET if form == SupplyLine.Form.SHEET else Roll.Form.ROLL,
                width=data.get("width"), height=data.get("height"),
                length=data.get("length"), sheet_count=data.get("sheet_count"),
                purchase_cost=cost, code=line.code, user=user,
                received_at=happened_at, supply=supply,
            )
        else:
            # Штучный материал партий не заводит — обычное движение склада с
            # ценой за единицу, чтобы закуп месяца посчитался как раньше.
            apply_stock_change(
                material, qty,
                log_type=InventoryLog.Type.SUPPLY,
                actual_price=(cost / qty).quantize(Decimal("0.01")) if qty else None,
                reason=f"{reason_head}: {material.name}",
                user=user, happened_at=happened_at, supply=supply,
            )

        line.save()

    return supply


@transaction.atomic
def unpost_supply(supply: Supply) -> None:
    """Отменить накладную и снять с остатков всё, что она принесла.

    Отменяем ТОЛЬКО нетронутую поставку: если из партии уже резали, откат
    сдвинул бы себестоимость закрытых заказов — тех самых, что уже посчитаны в
    прибыли. В таком случае честнее сказать «нельзя», чем тихо переписать
    прошлое (ровно по этой причине материал с продажами не удаляется, а
    прячется).
    """
    for line in supply.lines.select_related("material", "roll"):
        roll = line.roll
        if roll and roll.remaining_area != roll.initial_area:
            raise SupplyError(
                f"«{line.material.name}» из этой накладной уже резали — "
                "отменить её нельзя. Поправьте остаток инвентаризацией."
            )
        material = line.material
        if not material.is_roll_material and material.quantity < line.quantity:
            raise SupplyError(
                f"«{material.name}»: на складе осталось меньше, чем пришло по "
                "накладной, — часть уже продали. Отменить нельзя."
            )

    reason = f"Отмена накладной {supply.number or f'#{supply.pk}'}"
    for line in supply.lines.select_related("material", "roll"):
        material = line.material
        if line.roll:
            roll = line.roll
            line.roll = None
            line.save(update_fields=["roll"])
            roll.delete()
            # Партия ушла — снимаем её площадь с остатка материала.
            apply_stock_change(
                material, -line.quantity,
                log_type=InventoryLog.Type.ADJUSTMENT,
                reason=reason, user=None,
            )
        else:
            apply_stock_change(
                material, -line.quantity,
                log_type=InventoryLog.Type.ADJUSTMENT,
                reason=reason, user=None,
            )
    # Записи журнала этой накладной остаются: движение было, и стирать историю
    # склада нельзя. Отмена — это встречное движение, а не подчистка.
    supply.inventory_logs.update(supply=None)
    supply.delete()
