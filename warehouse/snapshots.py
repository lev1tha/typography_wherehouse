"""Снимок склада на конец дня и «склад на дату» (STK-04, волна 2).

Остаток материала на дату — сумма журнала склада по этот день (точно).
Раскладка по партиям — от сегодняшних остатков партий назад: то, что ушло после
даты, возвращается в партии с самой свежей (обратный FIFO), пришедшее после
даты — отнимается со старейшей. Для снимка, снятого в день закрытия месяца,
это и есть точное состояние; снятого позже — близкое к нему, но после снятия
цифра больше не плывёт от поставок, внесённых задним числом.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import InventoryLog, Material, StockSnapshot, StockSnapshotLine

ZERO = Decimal("0")


def _journal_qty(day) -> dict:
    return {
        row["material"]: row["v"] or ZERO
        for row in InventoryLog.objects.filter(happened_at__date__lte=day)
        .values("material").annotate(v=Sum("quantity_changed"))
    }


def lots_at(material: Material, day, qty_at: Decimal | None = None):
    """[(партия, кв.м)] и хвост сверх партий на конец дня `day`."""
    today = timezone.localdate()
    lots = sorted(
        (r for r in material.rolls.all() if timezone.localtime(r.received_at).date() <= day),
        key=lambda r: (r.received_at, r.pk),
    )
    areas = {r.pk: r.remaining_area for r in lots}
    if day >= today or qty_at is None:
        tail = (material.quantity or ZERO) - sum(areas.values(), ZERO)
        return [(r, areas[r.pk]) for r in lots], max(tail, ZERO)
    diff = qty_at - sum(areas.values(), ZERO)
    if diff > 0:
        for r in reversed(lots):           # ушло после даты — назад в свежие
            room = r.initial_area - areas[r.pk]
            give = min(room, diff)
            if give > 0:
                areas[r.pk] += give
                diff -= give
    elif diff < 0:
        need = -diff
        for r in lots:                     # вернулось после даты — со старейших
            take = min(areas[r.pk], need)
            areas[r.pk] -= take
            need -= take
        diff = ZERO
    return [(r, areas[r.pk]) for r in lots], max(diff, ZERO)


def _rows(day) -> list[dict]:
    qty = _journal_qty(day) if day < timezone.localdate() else None
    out = []
    for m in Material.objects.prefetch_related("rolls").order_by("name"):
        at = qty.get(m.pk, ZERO) if qty is not None else None
        if at is not None and at <= 0:
            continue
        if at is None and (m.quantity or ZERO) <= 0:
            continue
        lots, tail = lots_at(m, day, at)
        lines = [(r, a, r.cost_of(a)) for r, a in lots if a > 0]
        if tail > 0:
            lines.append((None, tail, tail * (m.purchase_price or ZERO)))
        quantity = sum((a for _r, a, _v in lines), ZERO)
        out.append({
            "material": m, "lines": lines, "quantity": quantity,
            "value": sum((v for *_x, v in lines), ZERO).quantize(Decimal("0.01")),
        })
    return out


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
    Открыли назад — снимки закрытия позже новой границы больше не заперты: их
    убираем, при новом закрытии снимутся заново."""
    limit = instance.closed_through
    stale = StockSnapshot.objects.filter(source="close")
    if limit:
        stale = stale.filter(as_of__gt=limit)
    stale.delete()
    if limit and not StockSnapshot.objects.filter(as_of=limit).exists():
        take_snapshot(limit, user=instance.updated_by, source="close")
