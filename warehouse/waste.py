"""Отход (брак) — списание материала теми же мерками, какими его принимают.

Владелец просил «раздел отходов в поступлении: точно так же, как приход по
размеру и по площади, но уже в отход». Приход считает лист как ширина × высота
× количество, рулон — метрами, штучное — количеством, и площадью, когда счёт
выставлен в квадратах. Отход вводится теми же цифрами: брак меряют той же
рулеткой, что и поставку, и переводить «два листа 1.22×2.44» в 5.95 кв.м в
уме — лишняя работа и лишний повод ошибиться.

Своей механики склада здесь нет: каждая строка уходит тем же путём, что и
обычное списание (`consume_area` FIFO по партиям, `write_off_roll` по рулону,
`apply_stock_change` у штучного без партий), пишется в журнал типом «Списание»
с причиной «Отход/брак» и СЕБЕСТОИМОСТЬЮ (`InventoryLog.cost`) — иначе на
вопрос «сколько денег выбросили за месяц» отвечать нечем.
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import InventoryLog, Material, Roll
from .rolls import InsufficientStock, compute_area, consume_area, has_lots, write_off_roll
from .stock import apply_stock_change


class WasteError(Exception):
    """Отход нельзя записать — с человеческим объяснением."""


FORM_SHEET = "SHEET"   # лист: ширина × высота × количество
FORM_AREA = "AREA"     # площадью: кв.м как есть
FORM_ROLL = "ROLL"     # рулон: метры с конкретного рулона
FORM_QTY = "QTY"       # штучный: количество
FORMS = (FORM_SHEET, FORM_AREA, FORM_ROLL, FORM_QTY)


def _moment(day):
    """Дата отхода → момент: полдень, как у накладной и заказа задним числом
    (полночь в Бишкеке — вчера по UTC)."""
    if not day:
        return None
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


def _n(value) -> str:
    """Число без хвоста нулей: «1.22», «2», не «2.00»."""
    return format(Decimal(str(value)).normalize(), "f")


def line_quantity(material: Material, form: str, *, width=None, height=None,
                  sheet_count=None, area=None, length=None, quantity=None,
                  roll: Roll | None = None) -> Decimal:
    """Сколько уйдёт со склада по строке — в единицах материала (кв.м у
    площадного, своя у штучного). Та же арифметика, что в сетке на экране."""
    if not material.is_roll_material:
        return Decimal(str(quantity or 0))
    if form == FORM_AREA:
        return Decimal(str(area or 0))
    if form == FORM_ROLL:
        w = (roll.width if roll is not None and roll.width else None) or material.roll_width
        if not w or not length:
            return Decimal("0")
        return (Decimal(str(length)) * Decimal(str(w))).quantize(Decimal("0.0001"))
    if not all((width, height, sheet_count)):
        return Decimal("0")
    return compute_area(Roll.Form.SHEET, width=width, height=height, sheet_count=sheet_count)


def dims_label(material: Material, form: str, *, width=None, height=None,
               sheet_count=None, area=None, length=None, quantity=None) -> str:
    """Как отход был введён — для причины в журнале: «лист 1.22×2.44 ×2»."""
    if not material.is_roll_material:
        return f"{_n(quantity or 0)} {material.get_unit_display()}"
    if form == FORM_AREA:
        return f"{_n(area or 0)} кв.м"
    if form == FORM_ROLL:
        return f"{_n(length or 0)} м"
    return f"лист {_n(width or 0)}×{_n(height or 0)} ×{_n(sheet_count or 0)}"


def _head(note_all: str, line_note: str) -> str:
    """Начало причины: «Отход/брак: зажевало в плоттере, разгрузка»."""
    text = "Отход/брак"
    detail = ", ".join(x for x in (line_note or "", note_all or "") if x)
    return f"{text}: {detail}" if detail else text


@transaction.atomic
def write_off_waste(lines: list[dict], *, user=None, happened_on=None, note: str = "") -> list[InventoryLog]:
    """Списать отход по строкам. Возвращает записи журнала — по одной на строку.

    Строка: material, form (SHEET|AREA|ROLL|QTY), width/height/sheet_count,
    area, length, roll, quantity, note. Нехватка на складе — InsufficientStock
    из примитивов, вся операция откатывается целиком: половина отхода в
    журнале, половина «не прошла» — хуже, чем ни одной.
    """
    if not lines:
        raise WasteError("Нет ни одной строки отхода.")
    moment = _moment(happened_on)
    entries: list[InventoryLog] = []

    for data in lines:
        material: Material = data["material"]
        form = data.get("form") or (
            FORM_QTY if not material.is_roll_material
            else FORM_ROLL if material.sells_by_metre
            else FORM_SHEET
        )
        roll = data.get("roll")
        if roll is not None and roll.material_id != material.id:
            raise WasteError(
                f"«{material.name}»: партия {roll.code or f'№{roll.pk}'} — другого материала."
            )
        head = _head(note, data.get("note") or "")
        before = InventoryLog.objects.filter(material=material).order_by("-id").values_list("id", flat=True).first() or 0

        if material.sells_by_metre:
            # Рулон: брак — с конкретного рулона и в метрах, его шириной и по
            # его цене (аудит 18.08, п. 7). Рулон не назвали — берём тот, что
            # FIFO списал бы первым; площадь переводим в метры его шириной.
            if roll is None:
                roll = (
                    Roll.objects.filter(material=material, remaining_area__gt=0, width__isnull=False)
                    .order_by("received_at").first()
                )
                if roll is None:
                    raise WasteError(f"«{material.name}»: нет рулонов с остатком — списывать не с чего.")
            if form == FORM_AREA:
                if not roll.width:
                    raise WasteError(f"«{material.name}»: у рулона не задана ширина — площадь в метры не перевести.")
                metres = (Decimal(str(data.get("area") or 0)) / roll.width).quantize(Decimal("0.01"))
            else:
                metres = Decimal(str(data.get("length") or 0))
            if metres <= 0:
                raise WasteError(f"«{material.name}»: укажите, сколько метров ушло в отход.")
            # Метры и рулон дописывает `write_off_roll` — здесь их не повторяем,
            # иначе причина читается как «… — 3 м Рулон №11: 3 м».
            write_off_roll(roll, metres, reason=f"{head} —", user=user, happened_at=moment)
        else:
            qty = line_quantity(
                material, form, width=data.get("width"), height=data.get("height"),
                sheet_count=data.get("sheet_count"), area=data.get("area"),
                length=data.get("length"), quantity=data.get("quantity"), roll=roll,
            )
            if qty <= 0:
                raise WasteError(
                    f"«{material.name}»: не из чего посчитать количество — проверьте "
                    "размеры или количество в строке."
                )
            # Чем мерили — в причину: «лист 1.22×2.44 ×2», «10 шт».
            reason = f"{head} — " + dims_label(
                material, form, width=data.get("width"), height=data.get("height"),
                sheet_count=data.get("sheet_count"), area=data.get("area"),
                length=data.get("length"), quantity=data.get("quantity"),
            )
            if material.is_roll_material or has_lots(material):
                # Лист и штучное с партиями — FIFO по партиям (выбранная —
                # первой), себестоимость по ним же.
                consume_area(
                    material, qty, user=user, reason=reason,
                    log_type=InventoryLog.Type.WRITE_OFF, happened_at=moment,
                    preferred_roll=roll,
                )
            else:
                # Штучный без партий: себестоимость — по закупочной из карточки,
                # другой у такого запаса нет (так же считает `stock_value`).
                apply_stock_change(
                    material, -qty, log_type=InventoryLog.Type.WRITE_OFF, reason=reason,
                    user=user, happened_at=moment,
                    cost=(qty * (material.purchase_price or Decimal("0"))).quantize(Decimal("0.01")),
                )

        entry = (
            InventoryLog.objects.filter(material=material, id__gt=before, type=InventoryLog.Type.WRITE_OFF)
            .order_by("-id").first()
        )
        if entry is not None:
            entries.append(entry)
    return entries


def waste_summary(entries: list[InventoryLog]) -> str:
    """Одной строкой для журнала действий: «Акрил 2мм −5.95 кв.м (8 000 сом); …»."""
    parts = []
    for e in entries:
        qty = -e.quantity_changed
        if e.metres_changed is not None:
            amount = f"{_n(-e.metres_changed)} м"
        else:
            unit = "кв.м" if e.material.is_roll_material else e.material.get_unit_display()
            amount = f"{_n(qty.quantize(Decimal('0.01')))} {unit}"
        cost = f", себест. {_n(e.cost.quantize(Decimal('1')))} сом" if e.cost is not None else ""
        parts.append(f"{e.material.name} {amount}{cost}")
    return "; ".join(parts)
