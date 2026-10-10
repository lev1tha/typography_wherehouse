"""Снимок склада на конец дня и «склад на дату» (STK-04, волна 2).

ОДНА функция стоимости склада на дату (RS-N1/STK-04, перепроверка 10.10):
ею считают «Склад на дату», `stock_value_total` («Финансы», начало и конец
цепочки «Сводки», «Обзор») и снимок при закрытии месяца. Раньше их было три,
и на конец сентября выходило три числа: 47 400, 49 800 и 48 360.

Остаток материала на дату — сумма журнала склада по этот день (точно).
Остаток ПАРТИИ на дату — сегодняшний остаток партии плюс то, что ушло из неё
после даты, минус то, что вернулось: движения журнала, привязанные к партиям
(`InventoryLogLot` — продажа, возврат, недостача, излишек, отход, списание,
промер, возврат поставщику датой возврата). Октябрьская недостача, ушедшая со
старейшей партии, сентябрь больше не двигает.

Остаток, который партиями не объясняется (движения до 10.10 без раскладки по
партиям, количество без партий), — как раньше: сначала «хвост сверх партий»
(по последней закупочной), остальное — назад в самые свежие партии, где есть
место. «Сейчас» это ровно `Material.stock_value`.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import InventoryLog, InventoryLogLot, Material, StockSnapshot, StockSnapshotLine

ZERO = Decimal("0")
CENT = Decimal("0.01")


def _journal_qty(day) -> dict:
    return {
        row["material"]: row["v"] or ZERO
        for row in InventoryLog.objects.filter(happened_at__date__lte=day)
        .values("material").annotate(v=Sum("quantity_changed"))
    }


def _moves_after(day) -> dict:
    """{партия: сумма её движений ПОСЛЕ дня `day`} — со знаком журнала (минус —
    ушло из партии, плюс — вернулось)."""
    out = defaultdict(lambda: ZERO)
    for row in (
        InventoryLogLot.objects.filter(log__happened_at__date__gt=day)
        .values("roll_id").annotate(v=Sum("area"))
    ):
        out[row["roll_id"]] += row["v"] or ZERO
    # Запись с партией, но без раскладки: всё её количество — этой партии.
    # Приход не в счёт: партия, пришедшая после даты, в расчёт не входит.
    for row in (
        InventoryLog.objects.filter(happened_at__date__gt=day, roll__isnull=False, lot_moves__isnull=True)
        .exclude(type=InventoryLog.Type.SUPPLY).exclude(quantity_changed=0)
        .values("roll_id").annotate(v=Sum("quantity_changed"))
    ):
        out[row["roll_id"]] += row["v"] or ZERO
    return out


def lots_at(material: Material, day, qty_at: Decimal | None = None, moves_after=None):
    """[(партия, кв.м)] и хвост сверх партий на конец дня `day`.

    ``qty_at`` — остаток материала на эту дату (по журналу); None — «сейчас».
    ``moves_after`` — `_moves_after(day)`; без него партии берутся как есть."""
    rolls = list(material.rolls.all())
    lots = sorted(
        (r for r in rolls if timezone.localtime(r.received_at).date() <= day),
        key=lambda r: (r.received_at, r.pk),
    )
    quantity = material.quantity or ZERO
    if qty_at is None:
        qty_at = quantity
    moves_after = moves_after or {}
    areas = {r.pk: max(r.remaining_area - moves_after.get(r.pk, ZERO), ZERO) for r in lots}
    diff = qty_at - sum(areas.values(), ZERO)
    tail = ZERO
    if diff > 0:
        # Сначала — хвост сверх партий: он и сегодня лежит вне партий.
        tail_now = max(quantity - sum((r.remaining_area for r in rolls), ZERO), ZERO)
        tail = min(diff, tail_now)
        diff -= tail
        # Ушло после даты без раскладки по партиям — назад в свежие.
        for r in reversed(lots):
            if diff <= 0:
                break
            give = min(r.initial_area - areas[r.pk], diff)
            if give > 0:
                areas[r.pk] += give
                diff -= give
        tail += max(diff, ZERO)
    elif diff < 0:
        # Партии знают больше остатка — как `Material.stock_value`: в пределах
        # остатка, со старейших.
        left = qty_at
        for r in lots:
            take = min(areas[r.pk], max(left, ZERO))
            areas[r.pk] = take
            left -= take
    return [(r, areas[r.pk]) for r in lots], tail


def _rows(day) -> list[dict]:
    """По материалам на конец дня `day`: строки (партия, кв.м, сом) и итог.

    Сумма строк материала равна его стоимости до тыйына: стоимость материала
    округляется один раз (как `Material.stock_value`), строки — каждая, а
    копейка округления ложится на последнюю. Поэтому снимок и расчёт дают одно
    и то же число."""
    past = day < timezone.localdate()
    if past:
        qty = _journal_qty(day)
        moves = _moves_after(day)
        materials = Material.objects.filter(pk__in=[pk for pk, v in qty.items() if v > 0])
    else:
        qty, moves = None, {}
        materials = Material.objects.filter(quantity__gt=0)
    out = []
    for m in materials.prefetch_related("rolls").order_by("name"):
        at = qty.get(m.pk, ZERO) if past else (m.quantity or ZERO)
        if at <= 0:
            continue
        lots, tail = lots_at(m, day, at, moves)
        raw = [(r, a, r.cost_of(a)) for r, a in lots if a > 0]
        if tail > 0:
            raw.append((None, tail, tail * (m.purchase_price or ZERO)))
        value = sum((v for *_x, v in raw), ZERO).quantize(CENT)
        lines = [(r, a, v.quantize(CENT)) for r, a, v in raw]
        if lines:
            drift = value - sum((v for *_x, v in lines), ZERO)
            if drift:
                r, a, v = lines[-1]
                lines[-1] = (r, a, v + drift)
        out.append({
            "material": m, "lines": lines,
            "quantity": sum((a for _r, a, _v in lines), ZERO),
            "value": value,
        })
    return out


def stock_value_on(day) -> Decimal:
    """Стоимость всего склада на конец дня `day` — расчётом (без снимка)."""
    return sum((r["value"] for r in _rows(day)), ZERO).quantize(CENT)


@transaction.atomic
def take_snapshot(day, *, user=None, source: str = "command") -> StockSnapshot:
    """Снять (пересъёмка — заменить) снимок склада на конец дня `day`."""
    StockSnapshot.objects.filter(as_of=day).delete()
    snap = StockSnapshot.objects.create(as_of=day, source=source, created_by=user)
    total = ZERO
    objs = []
    for row in _rows(day):
        for roll, area, value in row["lines"]:
            v = value.quantize(Decimal("0.01"))
            objs.append(StockSnapshotLine(snapshot=snap, material=row["material"], roll=roll,
                                          quantity=area.quantize(Decimal("0.0001")), value=v))
            total += v
    StockSnapshotLine.objects.bulk_create(objs)
    snap.value = total
    snap.save(update_fields=["value"])
    return snap


def stock_on_date(day) -> dict:
    """Склад на конец дня: из снимка, если он есть, иначе расчётом.

    {"as_of", "source": "snapshot"|"calc", "value", "rows": [{id, name,
    quantity, units, unit_label, value}]}."""
    from .reorder import unit_kind, unit_label

    snap = StockSnapshot.objects.filter(as_of=day).first()
    rows = []
    if snap is not None:
        per = defaultdict(lambda: [ZERO, ZERO])
        for line in snap.lines.all():
            per[line.material_id][0] += line.quantity
            per[line.material_id][1] += line.value
        materials = {m.pk: m for m in Material.objects.filter(pk__in=per)}
        source, total = "snapshot", snap.value
        items = [(materials[pk], q, v) for pk, (q, v) in per.items()]
    else:
        calc = _rows(day)
        source = "calc"
        total = sum((r["value"] for r in calc), ZERO)
        items = [(r["material"], r["quantity"], r["value"]) for r in calc]
    for m, quantity, value in sorted(items, key=lambda x: x[0].name):
        kind = unit_kind(m)
        units = quantity / m.piece_area if kind == "sheet" else quantity
        if kind == "pm" and m.roll_width:
            units = quantity / m.roll_width
        rows.append({
            "id": m.pk, "name": m.name, "quantity": quantity.quantize(Decimal("0.0001")),
            "units": units.quantize(Decimal("0.01")), "unit_label": unit_label(m),
            "value": value.quantize(Decimal("0.01")),
        })
    return {"as_of": day, "source": source, "value": total.quantize(Decimal("0.01")), "rows": rows}


def on_period_lock_saved(sender, instance, **kwargs):
    """Закрыли месяц — снять склад на дату закрытия (если снимка ещё нет).
    Снимается состояние на КОНЕЦ дня закрытия, а не партии в момент нажатия:
    тот же расчёт, что «Склад на дату» и `stock_value_total`, поэтому движения
    нового месяца, прошедшие до закрытия, в снимок не попадают, а пересъёмка
    командой `stock_snapshot --month` даёт то же число. Открыли назад — снимки закрытия позже новой границы больше не заперты: их
    убираем, при новом закрытии снимутся заново."""
    limit = instance.closed_through
    stale = StockSnapshot.objects.filter(source="close")
    if limit:
        stale = stale.filter(as_of__gt=limit)
    stale.delete()
    if limit and not StockSnapshot.objects.filter(as_of=limit).exists():
        take_snapshot(limit, user=instance.updated_by, source="close")
