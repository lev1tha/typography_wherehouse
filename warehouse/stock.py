"""Single source of truth for mutating material stock.

Every quantity change goes through here so that low-stock Telegram alerts and
inventory logging happen consistently — whether the change originates from a
sale, a return, a supply intake, or an inventory adjustment.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from .models import InventoryLog, Material
from .rolls import InsufficientStock


@transaction.atomic
def apply_stock_change(
    material: Material,
    delta: Decimal,
    *,
    allow_negative: bool = False,
    log_type: str | None = None,
    actual_price: Decimal | None = None,
    reason: str | None = None,
    user=None,
    happened_at=None,
    receipt=None,
    supply=None,
    cost: Decimal | None = None,
) -> Material:
    """Add `delta` (may be negative) to a material's quantity.

    Locks the row to avoid race conditions on concurrent sales. Optionally
    writes an InventoryLog entry and updates the purchase price. Fires a
    low-stock alert if the new quantity crosses the critical balance.

    ``happened_at`` — дата самой операции: приход часто вносят задним числом.
    Не передана — берётся текущий момент.

    Расход НИЖЕ НУЛЯ не пропускаем: у площадных материалов это давно ловит
    FIFO по партиям (``consume_area``), а штучные (крепёж, клей, бумага) уходили
    в минус молча — продажа 10 000 штук при остатке 484 создавала чек на
    550 000 сом и остаток −9 519. Отрицательный остаток ломает и стоимость
    склада, и себестоимость, и метку «нет в наличии».

    ``allow_negative=True`` — только для инвентаризации: там остаток не
    «уменьшается на», а ПРИРАВНИВАЕТСЯ к пересчитанному, и промежуточная
    проверка там не при чём (само значение уже проверено сериализатором).
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    was_above = locked.quantity > locked.critical_balance

    new_quantity = (locked.quantity or Decimal("0")) + Decimal(delta)
    if new_quantity < 0 and not allow_negative:
        raise InsufficientStock(
            f"Недостаточно «{locked.name}»: нужно {abs(Decimal(delta))}, "
            f"в наличии {locked.quantity}."
        )
    locked.quantity = new_quantity
    if actual_price is not None:
        locked.purchase_price = actual_price
    locked.save(update_fields=["quantity", "purchase_price", "updated_at"])

    if log_type:
        entry = InventoryLog(
            type=log_type,
            material=locked,
            quantity_changed=Decimal(delta),
            actual_price=actual_price,
            reason=reason,
            receipt=receipt,
            supply=supply,
            # Себестоимость ушедшего — только если вызывающий её знает
            # (штучный материал без партий: количество × закупочная).
            cost=cost,
            created_by=user,
        )
        if happened_at:
            entry.happened_at = happened_at
        entry.save()

    # Trigger alert only on a downward crossing of the critical threshold.
    if was_above and locked.quantity <= locked.critical_balance:
        _notify_low_stock(locked)

    return locked


def _notify_low_stock(material: Material) -> None:
    # Imported lazily to avoid a hard dependency during migrations/tests.
    from integrations.telegram import notify_low_stock

    notify_low_stock(material)
