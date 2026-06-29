"""Single source of truth for mutating material stock.

Every quantity change goes through here so that low-stock Telegram alerts and
inventory logging happen consistently — whether the change originates from a
sale, a return, a supply intake, or an inventory adjustment.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from .models import InventoryLog, Material


@transaction.atomic
def apply_stock_change(
    material: Material,
    delta: Decimal,
    *,
    log_type: str | None = None,
    actual_price: Decimal | None = None,
    reason: str | None = None,
    user=None,
) -> Material:
    """Add `delta` (may be negative) to a material's quantity.

    Locks the row to avoid race conditions on concurrent sales. Optionally
    writes an InventoryLog entry and updates the purchase price. Fires a
    low-stock alert if the new quantity crosses the critical balance.
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    was_above = locked.quantity > locked.critical_balance

    locked.quantity = (locked.quantity or Decimal("0")) + Decimal(delta)
    if actual_price is not None:
        locked.purchase_price = actual_price
    locked.save(update_fields=["quantity", "purchase_price", "updated_at"])

    if log_type:
        InventoryLog.objects.create(
            type=log_type,
            material=locked,
            quantity_changed=Decimal(delta),
            actual_price=actual_price,
            reason=reason,
            created_by=user,
        )

    # Trigger alert only on a downward crossing of the critical threshold.
    if was_above and locked.quantity <= locked.critical_balance:
        _notify_low_stock(locked)

    return locked


def _notify_low_stock(material: Material) -> None:
    # Imported lazily to avoid a hard dependency during migrations/tests.
    from integrations.telegram import notify_low_stock

    notify_low_stock(material)
