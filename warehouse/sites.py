"""Площадки хранения и перемещение между ними (STK-05/G4-N4, волна 2).

Модель — без деления партий. У партии есть площадка (`Roll.site`), а часть её
остатка, перевезённая на другую площадку, лежит строкой `LotPlacement`.
Правило чтения (`placements`): остаток партии сначала числится на её
площадке; перевезённое — сверху. Продажа площадку не знает и уходит с площадки
партии; когда там пусто, тают перевезённые части — по порядку перемещения.

Перемещение (`transfer`) не трогает ни остаток, ни цену, ни закуп, ни потери:
в журнал склада ложится строка «Перемещение» с нулевым количеством, документ —
`StockTransfer`.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import transaction

from .models import InventoryLog, LotPlacement, Material, ProductionSite, Roll, StockTransfer

ZERO = Decimal("0")


class TransferError(Exception):
    """Переместить нельзя — с человеческим объяснением."""


def placements(roll: Roll, shares=None) -> dict:
    """{id площадки или None: кв.м} — где лежит остаток партии сейчас."""
    rows = shares if shares is not None else list(roll.placements.all())
    left = roll.remaining_area
    out = defaultdict(lambda: ZERO)
    moved = []
    for p in sorted(rows, key=lambda p: p.pk or 0):
        if p.site_id == roll.site_id or p.area <= 0:
            continue
        moved.append(p)
    total_moved = sum((p.area for p in moved), ZERO)
    home = left - total_moved
    if home >= 0:
        if home > 0:
            out[roll.site_id] += home
        for p in moved:
            out[p.site_id] += p.area
        return dict(out)
    # Партии на «своей» площадке не осталось — тают перевезённые, старые первыми.
    short = -home
    for p in moved:
        cut = min(p.area, short)
        short -= cut
        if p.area - cut > 0:
            out[p.site_id] += p.area - cut
    return dict(out)


def _save_shares(roll: Roll, at: dict) -> None:
    """Записать раскладку партии: всё, что не на её площадке, — строками."""
    keep = {sid: area for sid, area in at.items() if sid != roll.site_id and area > 0}
    for p in list(roll.placements.all()):
        if p.site_id not in keep:
            p.delete()
    for sid, area in keep.items():
        LotPlacement.objects.update_or_create(roll=roll, site_id=sid, defaults={"area": area})


def site_stock(material: Material, names: dict | None = None) -> list[dict]:
    """Остаток и стоимость материала по площадкам: [{site, name, area, value}].

    Пусто, если площадки не указаны нигде (весь остаток «без площадки»)."""
    if names is None:
        names = {s.pk: s.name for s in ProductionSite.objects.all()}
    area = defaultdict(lambda: ZERO)
    value = defaultdict(lambda: ZERO)
    for roll in material.rolls.all():
        if roll.remaining_area <= 0:
            continue
        for sid, part in placements(roll).items():
            area[sid] += part
            value[sid] += roll.cost_of(part)
    in_lots = sum(area.values(), ZERO)
    tail = (material.quantity or ZERO) - in_lots
    if tail > 0:
        # Остаток сверх партий площадки не знает — «не указана».
        area[None] += tail
        value[None] += tail * (material.purchase_price or ZERO)
    if not any(sid is not None for sid in area):
        return []
    return [
        {"site": sid, "name": names.get(sid) if sid else None,
         "area": a.quantize(Decimal("0.0001")), "value": value[sid].quantize(Decimal("0.01"))}
        for sid, a in sorted(area.items(), key=lambda kv: (kv[0] is None, names.get(kv[0], "")))
        if a > 0
    ]


@transaction.atomic
def transfer(material: Material, area, *, from_site=None, to_site=None, roll: Roll | None = None,
             happened_on=None, note: str = "", user=None) -> StockTransfer:
    """Перевезти `area` кв.м (штук) материала с площадки на площадку.

    Берём партии FIFO (или выбранную), с той части каждой, что лежит на
    `from_site`. Не хватает — отказ целиком.
    """
    from .waste import _moment

    need = Decimal(str(area))
    if need <= 0:
        raise TransferError("Укажите, сколько перемещаете.")
    from_id = getattr(from_site, "pk", from_site)
    to_id = getattr(to_site, "pk", to_site)
    if from_id == to_id:
        raise TransferError("Откуда и куда — одна и та же площадка.")
    locked = Material.objects.select_for_update().get(pk=material.pk)
    lots = list(
        Roll.objects.select_for_update().filter(material=locked, remaining_area__gt=0)
        .prefetch_related("placements").order_by("received_at", "pk")
    )
    if roll is not None:
        lots = [r for r in lots if r.pk == roll.pk]
        if not lots:
            raise TransferError("В выбранной партии ничего не осталось.")
    cost = ZERO
    moved = ZERO
    for lot in lots:
        if need <= 0:
            break
        at = placements(lot)
        take = min(at.get(from_id, ZERO), need)
        if take <= 0:
            continue
        at[from_id] = at.get(from_id, ZERO) - take
        at[to_id] = at.get(to_id, ZERO) + take
        _save_shares(lot, at)
        cost += lot.cost_of(take)
        moved += take
        need -= take
    if need > 0:
        names = {s.pk: s.name for s in ProductionSite.objects.filter(pk__in=[x for x in (from_id,) if x])}
        where = names.get(from_id, "без площадки") if from_id else "без площадки"
        raise TransferError(
            f"«{locked.name}»: на площадке «{where}» только "
            f"{(moved).normalize():f} — переместить {Decimal(str(area)).normalize():f} нельзя."
        )
    doc = StockTransfer.objects.create(
        material=locked, from_site_id=from_id, to_site_id=to_id, area=moved,
        cost=cost.quantize(Decimal("0.01")), note=(note or "").strip()[:255], created_by=user,
        **({"happened_on": happened_on} if happened_on else {}),
    )
    names = {s.pk: s.name for s in ProductionSite.objects.filter(pk__in=[x for x in (from_id, to_id) if x])}
    label = lambda sid: names.get(sid, "без площадки") if sid else "без площадки"  # noqa: E731
    entry = InventoryLog(
        type=InventoryLog.Type.TRANSFER, material=locked, quantity_changed=ZERO,
        reason=(
            f"Перемещение {moved.normalize():f} "
            f"{'кв.м' if locked.is_roll_material else locked.get_unit_display()}: "
            f"{label(from_id)} → {label(to_id)}" + (f". {doc.note}" if doc.note else "")
        ),
        created_by=user,
    )
    moment = _moment(happened_on)
    if moment:
        entry.happened_at = moment
    entry.save()
    return doc
