"""Складской лист по материалам — расчёт как в Excel заказчика.

Формула его листа на каждый месяц:

    остаток на конец = остаток на начало + поступление − проданные

Заказчик всю жизнь вёл это в Excel и **остаток на начало переносил руками** с
прошлого месяца. Здесь система делает перенос сама: вписать значение нужно один
раз — в том месяце, с которого начинают вести учёт, — а дальше каждый следующий
месяц начинается с конца предыдущего. Вписанное вручную значение всегда
побеждает расчётное: инвентаризация или закупка мимо склада поправляются одной
цифрой, как в Excel.

Считать остаток «откатом» от текущего склада назад мы пробовали и отказались:
это требует, чтобы каждое движение за всю историю было записано без единой дыры
(продажи складского лога не пишут вовсе), а на живых данных цифры разъезжаются и
в таблице появляются отрицательные остатки.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models.functions import TruncMonth

from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, MaterialMonthOpening

ZERO = Decimal("0")
_CENT = Decimal("0.01")


def q2(value):
    """Округлить количество до сотых.

    Перевод кв.м в листы даёт бесконечную дробь (86.4 / 2.98), и без округления
    она уезжает в поле ввода как «54.35167785234899».
    """
    return Decimal(value).quantize(_CENT)


def counting_unit(material):
    """Площадь одной штуки, если материал считают листами, иначе None.

    Заказчик ведёт склад листами, а система хранит площадные материалы в кв.м.
    """
    area = material.piece_area
    return area if area and area > 0 else None


def to_units(material, value):
    """Перевести кв.м в листы, если материал считают листами."""
    per_sheet = counting_unit(material)
    return (value / per_sheet) if per_sheet else value


def _add_month(key):
    year, month = key
    return (year + 1, 1) if month == 12 else (year, month + 1)


def collect_flows(materials):
    """Поступления и продажи по месяцам, по каждому материалу, в единицах счёта.

    Возвращает (received, sold): {material_id: {(год, месяц): количество}}.
    """
    by_id = {m.id: m for m in materials}
    received = defaultdict(lambda: defaultdict(lambda: ZERO))
    sold = defaultdict(lambda: defaultdict(lambda: ZERO))

    supply = (
        InventoryLog.objects.filter(type=InventoryLog.Type.SUPPLY, quantity_changed__gt=0)
        .annotate(m=TruncMonth("happened_at"))
        .values("material_id", "quantity_changed", "m")
    )
    for row in supply:
        material = by_id.get(row["material_id"])
        if not material:
            continue
        key = (row["m"].year, row["m"].month)
        received[material.id][key] += to_units(material, row["quantity_changed"])

    items = (
        TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL, is_returned=False, material__isnull=False
        )
        .exclude(receipt__status=Receipt.Status.CANCELLED)
        .select_related("material", "receipt")
    )
    for item in items:
        material = by_id.get(item.material_id)
        if not material:
            continue
        # Продажа листом целиком уже хранится в штуках — это и есть единица
        # счёта; продажа по площади хранится в кв.м и требует перевода.
        qty = item.quantity
        if item.sale_mode != TransactionItem.SaleMode.PIECE:
            qty = to_units(material, qty)
        day = item.receipt.created_at.date()
        sold[material.id][(day.year, day.month)] += qty

    return received, sold


def purchases_from_stock(d_from=None, d_to=None):
    """Сколько закуплено материала за период — по приходам на склад.

    Каждое поступление уже несёт свою себестоимость: и партия рулона/листа
    (`receive_lot` пишет цену за кв.м), и обычный приход по количеству. Раньше
    эту сумму заказчик вбивал в отчёт руками, хотя система её знала.

    Приходы без указанной цены пропускаем — сумму по ним не выдумываем.
    """
    qs = InventoryLog.objects.filter(
        type=InventoryLog.Type.SUPPLY, quantity_changed__gt=0, actual_price__isnull=False
    )
    if d_from:
        qs = qs.filter(happened_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(happened_at__date__lte=d_to)
    return sum(
        (row["quantity_changed"] * row["actual_price"]
         for row in qs.values("quantity_changed", "actual_price")),
        ZERO,
    )


def collect_manual(materials):
    """Вручную вписанные остатки: {material_id: {(год, месяц): количество}}."""
    manual = defaultdict(dict)
    ids = [m.id for m in materials]
    for row in MaterialMonthOpening.objects.filter(material_id__in=ids).values(
        "material_id", "year", "month", "quantity"
    ):
        manual[row["material_id"]][(row["year"], row["month"])] = row["quantity"]
    return manual


def opening_for(material_id, target, *, manual, received, sold):
    """Остаток на начало месяца `target` = (год, месяц).

    Вписанное вручную значение этого месяца побеждает. Иначе берём ближайший
    более ранний ручной месяц и прокручиваем формулу вперёд до цели; если
    ручных значений вообще нет — считаем с нуля от первого движения.

    Возвращает (количество, вписано_ли_руками).
    """
    own = manual.get(material_id, {})
    if target in own:
        return own[target], True

    earlier = [key for key in own if key < target]
    if earlier:
        cursor = max(earlier)
        value = own[cursor]
    else:
        # Ручного якоря нет — накапливаем с нуля от самого раннего движения.
        moves = list(received.get(material_id, {})) + list(sold.get(material_id, {}))
        moves = [key for key in moves if key < target]
        if not moves:
            return ZERO, False
        cursor = min(moves)
        value = ZERO

    while cursor < target:
        value = (
            value
            + received.get(material_id, {}).get(cursor, ZERO)
            - sold.get(material_id, {}).get(cursor, ZERO)
        )
        cursor = _add_month(cursor)
        # Ручное значение промежуточного месяца перебивает накопленное.
        if cursor in own:
            value = own[cursor]
    return value, False
