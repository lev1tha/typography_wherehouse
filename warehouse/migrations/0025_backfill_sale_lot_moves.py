"""Раскладка по партиям для уже проведённых продаж (RS-N1, перепроверка 10.10).

Остаток партии на прошлую дату считается по движениям, привязанным к
партиям (`InventoryLogLot`). Новые движения пишут раскладку сами; у продаж,
проведённых раньше, она есть в `TransactionItemLot` (строка чека → партии) —
переносим её к записи журнала этой строки. Только однозначные случаи: одна
запись «Продажа» на строку и материал, и партий взято не больше, чем ушло по
записи. Остальное (возвраты, отход и инвентаризация до 10.10) раскладки не
имеет и считается, как раньше, — «назад в самые свежие партии».

Данные только добавляются; откат ничего не делает (таблица уходит вместе с
0024).
"""
from collections import defaultdict
from decimal import Decimal

from django.db import migrations

TINY = Decimal("0.0001")


def forward(apps, schema_editor):
    InventoryLog = apps.get_model("warehouse", "InventoryLog")
    InventoryLogLot = apps.get_model("warehouse", "InventoryLogLot")
    TransactionItemLot = apps.get_model("sales", "TransactionItemLot")

    logs = defaultdict(list)
    for log in (
        InventoryLog.objects.filter(type="SALE", receipt_item__isnull=False, lot_moves__isnull=True)
        .values("id", "receipt_item_id", "material_id", "quantity_changed")
    ):
        logs[(log["receipt_item_id"], log["material_id"])].append(log)
    if not logs:
        return
    uses = defaultdict(list)
    for use in (
        TransactionItemLot.objects.filter(
            item_id__in={key[0] for key in logs}, roll__isnull=False,
        ).values("item_id", "roll_id", "roll__material_id", "area")
    ):
        uses[(use["item_id"], use["roll__material_id"])].append(use)
    rows = []
    for key, group in logs.items():
        if len(group) != 1 or key not in uses:
            continue
        log = group[0]
        taken = sum((u["area"] for u in uses[key]), Decimal("0"))
        if taken <= 0 or taken > -log["quantity_changed"] + TINY:
            continue
        for u in uses[key]:
            if u["area"]:
                rows.append(InventoryLogLot(log_id=log["id"], roll_id=u["roll_id"], area=-u["area"]))
    InventoryLogLot.objects.bulk_create(rows, batch_size=500)


def backward(apps, schema_editor):
    # Добавленные строки отличить от записанных потом нельзя, а схема всё
    # равно уходит предыдущей миграцией вместе с таблицей.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("warehouse", "0024_inventory_log_lots_forward_returns"),
        ("sales", "0015_idempotency_item_lots_indexes"),
    ]

    operations = [migrations.RunPython(forward, backward)]
