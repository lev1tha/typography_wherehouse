"""«Пересчитать себестоимость по FIFO» с даты (PNL-08, волна 2).

Поставку внесли задним числом: партия 25.08 появилась 10.09, а продажи 05.09
уже списали более позднюю партию — по другой цене. Себестоимость сентября
расходилась с FIFO, а в «Сводке» висел необъяснимый разрыв склада.

Пересчёт — по одному материалу, начиная с даты: продажи с этой даты (по
записям «из какой партии взято», `TransactionItemLot`) раскладываются по
партиям заново — старейшая первой, только партии, поступившие не позже
продажи. Всё, что не продажа (отход, инвентаризация), остаётся там, где было.
Меняются: партии строк, себестоимость строк и остатки партий. Закуп и
количество не меняются.

Как «Исправить приход» (`lot_correction`): предпросмотр ничего не пишет;
применение — одной транзакцией; затронутые закрытые месяцы → отказ с
перечнем. Рулон метрами не пересчитывается: его режут с конкретного рулона
на полке, и FIFO по датам там физически неверен.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog

from .models import InventoryLog, InventoryLogLot, Material, Roll
from .rolls import record_lot_moves

CENT = Decimal("0.01")
ZERO = Decimal("0")
TINY = Decimal("0.000001")


class RecalcError(Exception):
    def __init__(self, message, *, closed_months=None):
        super().__init__(message)
        self.closed_months = closed_months or []


def _money(v) -> Decimal:
    return Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def _local(v):
    return timezone.localtime(v).date() if v else None


def _plan(material: Material, since) -> dict:
    from finance.periods import is_closed
    from sales.models import Receipt, TransactionItemLot

    if material.sells_by_metre:
        raise RecalcError(
            f"«{material.name}» режут с конкретного рулона на полке — пересчёт по "
            "датам FIFO для рулонов не делается."
        )
    lots = list(Roll.objects.filter(material=material).order_by("received_at", "pk"))
    by_pk = {r.pk: r for r in lots}
    start = timezone.make_aware(datetime.combine(since, time.min))
    uses = list(
        TransactionItemLot.objects.filter(
            roll__material=material, item__is_returned=False, item__receipt__created_at__gte=start,
        )
        .exclude(item__receipt__status=Receipt.Status.CANCELLED)
        .select_related("item__receipt")
        .order_by("item__receipt__created_at", "item_id", "id")
    )
    per_item = defaultdict(list)
    items = {}
    for u in uses:
        per_item[u.item_id].append(u)
        items[u.item_id] = u.item
    capacity = {r.pk: r.remaining_area for r in lots}
    for u in uses:
        if u.roll_id in capacity:
            capacity[u.roll_id] += u.area

    changes, new_alloc = [], {}
    for item_id, rows in per_item.items():
        item = items[item_id]
        moment = item.receipt.created_at
        need = sum((u.area for u in rows), ZERO)
        order = [r for r in lots if r.received_at <= moment] + [r for r in lots if r.received_at > moment]
        alloc = []
        for r in order:
            if need <= TINY:
                break
            take = min(capacity[r.pk], need)
            if take <= 0:
                continue
            capacity[r.pk] -= take
            alloc.append((r.pk, take))
            need -= take
        if need > TINY:
            raise RecalcError(
                f"«{material.name}»: партиям не хватает материала на продажи с "
                f"{since:%d.%m.%Y} — пересчёт невозможен (проверьте остатки)."
            )
        old = sorted((u.roll_id, u.area) for u in rows)
        new = sorted(alloc)
        new_alloc[item_id] = alloc
        if [(a, b.quantize(Decimal("0.000001"))) for a, b in old] == [
            (a, b.quantize(Decimal("0.000001"))) for a, b in new
        ]:
            continue
        before = sum((by_pk[pk].cost_of(a) for pk, a in old if pk in by_pk), ZERO)
        after = sum((by_pk[pk].cost_of(a) for pk, a in new), ZERO)
        delta = _money(after) - _money(before)
        changes.append({"item": item, "old": old, "new": new, "delta": delta})

    lot_rows = []
    for r in lots:
        if capacity[r.pk] != r.remaining_area:
            lot_rows.append({"lot": r, "before": r.remaining_area, "after": capacity[r.pk]})
    closed = sorted({
        f"{d.year:04d}-{d.month:02d}"
        for d in (_local(c["item"].receipt.revenue_recognized_at) for c in changes if c["delta"])
        if d and is_closed(d)
    })
    return {
        "material": material, "since": since, "changes": changes, "lots": lot_rows,
        "new_alloc": new_alloc, "closed_months": closed,
        "cogs_delta": sum((c["delta"] for c in changes), ZERO),
    }


def public(plan: dict) -> dict:
    def lot_label(pk):
        r = Roll.objects.filter(pk=pk).first()
        return (r.code or f"№{pk}") if r else f"№{pk}"

    return {
        "material": plan["material"].pk,
        "material_name": plan["material"].name,
        "since": plan["since"].isoformat(),
        "cogs_delta": plan["cogs_delta"],
        "closed_months": plan["closed_months"],
        "items": [
            {
                "item": c["item"].pk,
                "order_number": c["item"].receipt.order_number,
                "date": (_local(c["item"].receipt.revenue_recognized_at)
                         or _local(c["item"].receipt.created_at)).isoformat(),
                "lots_before": [{"lot": lot_label(pk), "area": a} for pk, a in c["old"]],
                "lots_after": [{"lot": lot_label(pk), "area": a} for pk, a in c["new"]],
                "cost_before": c["item"].cost_total,
                "cost_after": _money(c["item"].cost_total + c["delta"]),
                "delta": c["delta"],
            }
            for c in plan["changes"]
        ],
        "lots": [
            {"lot": r["lot"].code or f"№{r['lot'].pk}", "remaining_before": r["before"],
             "remaining_after": r["after"]}
            for r in plan["lots"]
        ],
    }


def preview(material: Material, since) -> dict:
    with transaction.atomic():
        return public(_plan(material, since))


@transaction.atomic
def apply(material: Material, since, *, user=None) -> dict:
    from sales.models import TransactionItem, TransactionItemLot

    material = Material.objects.select_for_update().get(pk=material.pk)
    list(Roll.objects.select_for_update().filter(material=material))
    plan = _plan(material, since)
    if plan["closed_months"]:
        months = ", ".join(f"{m[5:]}.{m[:4]}" for m in plan["closed_months"])
        raise RecalcError(
            f"Пересчёт меняет себестоимость закрытых месяцев: {months}. "
            "Откройте период в Финансах или выберите дату позже.",
            closed_months=plan["closed_months"],
        )
    if not plan["changes"]:
        raise RecalcError("По FIFO всё уже так — пересчитывать нечего.")
    ids = [c["item"].pk for c in plan["changes"]]
    locked = {it.pk: it for it in TransactionItem.objects.select_for_update().filter(pk__in=ids)}
    rolls = {r.pk: r for r in Roll.objects.filter(material=material)}
    for c in plan["changes"]:
        item = locked[c["item"].pk]
        item.cost_total = _money(item.cost_total + c["delta"])
        # Строка материала помнит первую партию (по ней возврат и подпись
        # «списано с …»); у услуги партия — у расходника, её не трогаем.
        if item.type == TransactionItem.Type.MATERIAL and c["new"]:
            item.roll_id = c["new"][0][0]
        item.save(update_fields=["cost_total", "roll"])
        TransactionItemLot.objects.filter(item=item, roll__material=material).delete()
        TransactionItemLot.objects.bulk_create(
            TransactionItemLot(item=item, roll_id=pk, area=a) for pk, a in plan["new_alloc"][item.pk]
        )
        # Партии записи журнала этой продажи — туда же: по ним считается
        # остаток партии на прошлую дату (склад на дату, снимок месяца).
        sale_logs = list(
            InventoryLog.objects.filter(
                receipt_item=item, material=material, type=InventoryLog.Type.SALE,
            ).order_by("id")
        )
        if sale_logs:
            InventoryLogLot.objects.filter(log__in=sale_logs, roll__material=material).delete()
            record_lot_moves(sale_logs[0], [(pk, -a) for pk, a in plan["new_alloc"][item.pk]])
    for row in plan["lots"]:
        lot = rolls[row["lot"].pk]
        lot.remaining_area = row["after"]
        lot.save(update_fields=["remaining_area"])
    delta = plan["cogs_delta"]
    text = (
        f"Пересчёт себестоимости по FIFO «{material.name}» с {since:%d.%m.%Y}: строк "
        f"{len(plan['changes'])}, себестоимость {'+' if delta >= 0 else ''}{delta} сом"
    )
    InventoryLog.objects.create(
        type=InventoryLog.Type.CORRECTION, material=material, quantity_changed=ZERO,
        reason=text, created_by=user,
    )
    AuditLog.record(user, text, kind="stock")
    return public(plan)
