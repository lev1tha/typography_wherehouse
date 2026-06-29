"""Roll (lot) intake and FIFO area consumption for roll-materials.

Roll materials are stocked and sold by area (кв.м). Each received roll keeps
its own cost and markup; the material's retail price-per-кв.м tracks the most
recent roll. Sales consume area oldest-roll-first (FIFO).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from .models import InventoryLog, Material, Roll


def compute_area(form: str, *, width=None, length=None, height=None, sheet_count=None) -> Decimal:
    """Area in кв.м for a lot, from its form and dimensions."""
    if form == Roll.Form.SHEET:
        return (Decimal(width) * Decimal(height) * Decimal(sheet_count)).quantize(Decimal("0.01"))
    return (Decimal(width) * Decimal(length)).quantize(Decimal("0.01"))


@transaction.atomic
def receive_lot(
    material: Material,
    *,
    form: str,
    purchase_cost: Decimal,
    markup_percent: Decimal,
    width=None,
    length=None,
    height=None,
    sheet_count=None,
    area: Decimal = None,
    code: str = "",
    user=None,
) -> Roll:
    """Receive a new lot (roll or sheets). Computes area from dimensions unless
    `area` is given directly; then creates the lot and refreshes material stock.
    """
    if area is None:
        area = compute_area(form, width=width, length=length, height=height, sheet_count=sheet_count)
    area = Decimal(area)
    locked = Material.objects.select_for_update().get(pk=material.pk)
    roll = Roll.objects.create(
        material=locked,
        code=code,
        form=form,
        width=width,
        length=length,
        height=height,
        sheet_count=sheet_count,
        initial_area=area,
        remaining_area=area,
        purchase_cost=Decimal(purchase_cost),
        markup_percent=Decimal(markup_percent),
        created_by=user,
    )
    # The material is a roll-material; stock is the sum of remaining roll areas.
    locked.is_roll_material = True
    if locked.unit != Material.Unit.SQM:
        locked.unit = Material.Unit.SQM
    locked.quantity = (locked.quantity or Decimal("0")) + Decimal(area)
    # Intake records cost only; the RETAIL price (price_per_sqm) is set by the
    # admin on the pricing page — the storekeeper never sets markup/retail.
    locked.purchase_price = roll.cost_per_sqm
    locked.save(update_fields=[
        "is_roll_material", "unit", "quantity", "purchase_price", "updated_at",
    ])

    InventoryLog.objects.create(
        type=InventoryLog.Type.SUPPLY,
        material=locked,
        quantity_changed=Decimal(area),
        actual_price=roll.cost_per_sqm,
        reason=f"Поступление: {roll.dimensions_label} ({area} кв.м), {purchase_cost} сом",
        created_by=user,
    )
    return roll


# Backwards-compatible alias.
def receive_roll(material, *, area, purchase_cost, markup_percent, code="", user=None):
    return receive_lot(
        material, form=Roll.Form.ROLL, area=area, purchase_cost=purchase_cost,
        markup_percent=markup_percent, code=code, user=user,
    )


class InsufficientStock(Exception):
    pass


@transaction.atomic
def consume_area(
    material: Material,
    area: Decimal,
    *,
    user=None,
    reason: str = "",
    log_type: str = InventoryLog.Type.ADJUSTMENT,
) -> Decimal:
    """Consume `area` кв.м from a roll-material, FIFO across rolls.

    Returns the total cost of goods consumed. Raises InsufficientStock if there
    is not enough remaining area across all rolls.
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    need = Decimal(area)
    if need <= 0:
        return Decimal("0")
    if locked.quantity < need:
        raise InsufficientStock(
            f"Недостаточно «{locked.name}»: нужно {need} кв.м, в наличии {locked.quantity}."
        )

    was_above = locked.quantity > locked.critical_balance
    cogs = Decimal("0")
    remaining = need
    rolls = Roll.objects.select_for_update().filter(
        material=locked, remaining_area__gt=0
    ).order_by("received_at")
    for roll in rolls:
        if remaining <= 0:
            break
        take = min(roll.remaining_area, remaining)
        roll.remaining_area -= take
        roll.save(update_fields=["remaining_area"])
        cogs += take * roll.cost_per_sqm
        remaining -= take

    locked.quantity -= need
    locked.save(update_fields=["quantity", "updated_at"])

    if reason:
        InventoryLog.objects.create(
            type=log_type,
            material=locked,
            quantity_changed=-need,
            reason=reason,
            created_by=user,
        )

    if was_above and locked.quantity <= locked.critical_balance:
        from integrations.telegram import notify_low_stock
        notify_low_stock(locked)

    return cogs


@transaction.atomic
def restore_area(material: Material, area: Decimal, *, user=None, reason: str = "") -> None:
    """Return `area` кв.м back to stock (refund). Tops up the most recent roll."""
    locked = Material.objects.select_for_update().get(pk=material.pk)
    add = Decimal(area)
    if add <= 0:
        return
    roll = Roll.objects.select_for_update().filter(material=locked).order_by("-received_at").first()
    if roll:
        roll.remaining_area += add
        roll.save(update_fields=["remaining_area"])
    locked.quantity += add
    locked.save(update_fields=["quantity", "updated_at"])
    if reason:
        InventoryLog.objects.create(
            type=InventoryLog.Type.ADJUSTMENT,
            material=locked,
            quantity_changed=add,
            reason=reason,
            created_by=user,
        )
