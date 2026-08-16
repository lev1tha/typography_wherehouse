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
    """Area in кв.м for a lot, from its form and dimensions.

    Округление до десятитысячных, как у площади листа. При сотых лист
    1.22 × 2.44 = 2.9768 и партия из 5 листов давала 14.88 вместо 14.884 —
    обратный пересчёт возвращал 4.9987 листа вместо пяти ровно в той колонке,
    которую заказчик сверяет со своим Excel. У партии из 50 листов расхождения
    не было (148.84 укладывается в два знака), поэтому на глаз проблема ловилась
    через раз.
    """
    if form == Roll.Form.SHEET:
        return (Decimal(width) * Decimal(height) * Decimal(sheet_count)).quantize(Decimal("0.0001"))
    return (Decimal(width) * Decimal(length)).quantize(Decimal("0.0001"))


@transaction.atomic
def receive_lot(
    material: Material,
    *,
    form: str,
    purchase_cost: Decimal,
    width=None,
    length=None,
    height=None,
    sheet_count=None,
    area: Decimal = None,
    code: str = "",
    user=None,
    received_at=None,
    supply=None,
) -> Roll:
    """Receive a new lot (roll or sheets). Computes area from dimensions unless
    `area` is given directly; then creates the lot and refreshes material stock.

    ``received_at`` — дата поступления; поставки часто вносят задним числом.
    По ней же идёт FIFO, поэтому партия встаёт в очередь по своей настоящей
    дате, а не по моменту ввода.
    """
    if area is None:
        area = compute_area(form, width=width, length=length, height=height, sheet_count=sheet_count)
    area = Decimal(area)
    locked = Material.objects.select_for_update().get(pk=material.pk)
    roll = Roll(
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
        created_by=user,
    )
    if received_at:
        roll.received_at = received_at
    roll.save()
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

    entry = InventoryLog(
        type=InventoryLog.Type.SUPPLY,
        material=locked,
        quantity_changed=Decimal(area),
        actual_price=roll.cost_per_sqm,
        reason=f"Поступление: {roll.dimensions_label} ({area} кв.м), {purchase_cost} сом",
        created_by=user,
        # Накладная, если приход пришёл документом, а не одиночной кнопкой.
        supply=supply,
    )
    if received_at:
        entry.happened_at = received_at
    entry.save()
    return roll


# Backwards-compatible alias.
def receive_roll(material, *, area, purchase_cost, code="", user=None):
    return receive_lot(
        material, form=Roll.Form.ROLL, area=area, purchase_cost=purchase_cost,
        code=code, user=user,
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
    log_type: str | None = None,
    receipt=None,
    happened_at=None,
) -> Decimal:
    """Consume `area` кв.м from a roll-material, FIFO across rolls.

    Returns the total cost of goods consumed. Raises InsufficientStock if there
    is not enough remaining area across all rolls.

    Запись в журнал делается по ``log_type`` — как в ``apply_stock_change``.
    Раньше она стояла под непустым ``reason``, и продажа рулонного материала
    (которая причину не передавала) уходила со склада незаметно для журнала.

    ``happened_at`` — дата операции (заказ мог быть оформлен задним числом). Она
    попадает В ЖУРНАЛ, но НЕ меняет выбор партий: списываем из тех рулонов,
    которые лежат на складе сейчас, а не из тех, что лежали на ту дату. Отматывать
    склад назад пришлось бы по всей истории движений, и на живых данных это
    расходится (см. остаток на начало месяца в складском листе).
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

    if log_type:
        entry = InventoryLog(
            type=log_type,
            material=locked,
            quantity_changed=-need,
            reason=reason,
            receipt=receipt,
            created_by=user,
        )
        if happened_at:
            entry.happened_at = happened_at
        entry.save()

    if was_above and locked.quantity <= locked.critical_balance:
        from integrations.telegram import notify_low_stock
        notify_low_stock(locked)

    return cogs


@transaction.atomic
def restore_area(
    material: Material,
    area: Decimal,
    *,
    user=None,
    reason: str = "",
    log_type: str | None = None,
    receipt=None,
    happened_at=None,
) -> None:
    """Return `area` кв.м back to stock (refund).

    Mirrors the FIFO drawdown: refills lots oldest-first, each only up to its
    original capacity (initial_area), so a refund spanning several lots restores
    them in the same order they were consumed and never inflates a roll past its
    initial area. Any surplus that no lot can hold (e.g. restoring more than was
    consumed) lands on the newest roll so material.quantity stays consistent with
    the sum of roll remainders.
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    add = Decimal(area)
    if add <= 0:
        return
    rolls = list(
        Roll.objects.select_for_update().filter(material=locked).order_by("received_at")
    )
    remaining = add
    for roll in rolls:
        if remaining <= 0:
            break
        headroom = roll.initial_area - roll.remaining_area
        if headroom <= 0:
            continue
        give = min(headroom, remaining)
        roll.remaining_area += give
        roll.save(update_fields=["remaining_area"])
        remaining -= give
    if remaining > 0 and rolls:
        newest = rolls[-1]
        newest.remaining_area += remaining
        newest.save(update_fields=["remaining_area"])
    locked.quantity += add
    locked.save(update_fields=["quantity", "updated_at"])
    if log_type:
        InventoryLog.objects.create(
            type=log_type,
            material=locked,
            quantity_changed=add,
            reason=reason,
            receipt=receipt,
            created_by=user,
            **({"happened_at": happened_at} if happened_at else {}),
        )
