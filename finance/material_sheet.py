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

from django.db.models import Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, MaterialMonthOpening, Roll, SupplyLine

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
    Рулон листами не считают, даже если у карточки задан размер листа: его
    меряют метрами (см. `sheet_unit`).
    """
    if material.sells_by_metre:
        return None
    area = material.piece_area
    return area if area and area > 0 else None


def sheet_unit(material) -> str:
    """В чём материал стоит в складском листе: `METER` — рулон (погонные
    метры), `SHEET` — лист (по площади листа), иначе собственная единица.

    Раньше рулон с размером листа в карточке (1.2×2) считался ЛИСТАМИ, а метры
    проданных METER-строк складывались как кв.м: «продано 1 м» превращалось в
    «1.000 кв.м / 0.42 листа». Владелец рулон меряет метрами — в них и лист.
    """
    if material.sells_by_metre:
        return "METER"
    return "SHEET" if counting_unit(material) else material.unit


def to_units(material, value, *, width=None):
    """Перевести кв.м в единицу листа: листы — по площади листа, метры — по
    ширине полотна (партии, если она известна, иначе карточки)."""
    if material.sells_by_metre:
        w = width or material.roll_width
        return (value / w) if w else value
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

    metre_ids = {m.id for m in materials if m.sells_by_metre}
    supply = (
        InventoryLog.objects.filter(type=InventoryLog.Type.SUPPLY, quantity_changed__gt=0)
        .annotate(m=TruncMonth("happened_at"))
        .values("material_id", "quantity_changed", "m")
    )
    for row in supply:
        material = by_id.get(row["material_id"])
        if not material or material.id in metre_ids:
            continue
        key = (row["m"].year, row["m"].month)
        received[material.id][key] += to_units(material, row["quantity_changed"])
    # Рулон приходит партией: метры считаем от ШИРИНЫ ПАРТИИ (в журнале — только
    # площадь, а ширина у партий разная), поэтому берём сами партии.
    for roll in Roll.objects.filter(material_id__in=metre_ids).values(
        "material_id", "initial_area", "width", "received_at"
    ):
        material = by_id[roll["material_id"]]
        day = timezone.localtime(roll["received_at"])
        received[material.id][(day.year, day.month)] += to_units(
            material, roll["initial_area"], width=roll["width"]
        )

    items = (
        TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL, is_returned=False, material__isnull=False
        )
        .exclude(receipt__status=Receipt.Status.CANCELLED)
        .select_related("material", "receipt", "roll")
    )
    for item in items:
        material = by_id.get(item.material_id)
        if not material:
            continue
        # Продажа листом целиком уже хранится в штуках — это и есть единица
        # счёта; рулон метрами — в метрах; продажа по площади хранится в кв.м
        # и требует перевода (у рулона — по ширине партии строки).
        qty = item.quantity
        if item.sale_mode == TransactionItem.SaleMode.PIECE:
            pass
        elif item.sale_mode == TransactionItem.SaleMode.METER:
            pass
        elif material.sells_by_metre:
            qty = to_units(material, qty, width=item.roll_width)
        else:
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
    # Приход ДОКУМЕНТОМ несёт точную сумму строки — её и берём. Через цену за
    # единицу та же поставка давала копеечный «хвост»: 48000 / 29.768 кв.м
    # округляется до 1612.47, обратно даёт 48000.01, и закуп переставал сходиться
    # с накладной поставщика ровно там, где заказчик их и сверяет.
    return sum(purchases_from_stock_by_day(d_from, d_to).values(), ZERO)


def purchases_from_stock_by_day(d_from=None, d_to=None) -> dict:
    """То же, что `purchases_from_stock`, но по дням: {дата: сумма}.

    Нужно графику «По дням»: он считает прибыль дня по ТОМУ ЖЕ правилу, что и
    плитки месяца, а закуп у плиток складывается из приходов на склад. Пока
    график брал только записи трат, на одном экране стояли две «Прибыли»:
    −14 265 сверху и +3 735 в графике. Один источник — одна цифра.
    """
    out = defaultdict(lambda: ZERO)
    lines = SupplyLine.objects.all()
    if d_from:
        lines = lines.filter(supply__received_on__gte=d_from)
    if d_to:
        lines = lines.filter(supply__received_on__lte=d_to)
    for row in lines.values("supply__received_on").annotate(v=Sum("cost")):
        out[row["supply__received_on"]] += row["v"] or ZERO

    # Всё остальное — одиночные приходы: там суммы нет, есть цена за единицу.
    qs = InventoryLog.objects.filter(
        type=InventoryLog.Type.SUPPLY, quantity_changed__gt=0,
        actual_price__isnull=False, supply__isnull=True,
    )
    if d_from:
        qs = qs.filter(happened_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(happened_at__date__lte=d_to)
    for row in qs.values("happened_at", "quantity_changed", "actual_price"):
        day = timezone.localtime(row["happened_at"]).date()
        out[day] += row["quantity_changed"] * row["actual_price"]
    return dict(out)


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
