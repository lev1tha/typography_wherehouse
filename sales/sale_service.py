"""Core sales business logic: build a receipt, deduct stock, handle payment
confirmation and refunds. Kept separate from the views so it can be reused by
the payment webhook and tested in isolation.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from warehouse.models import InventoryLog
from warehouse.rolls import consume_area, restore_area
from warehouse.stock import apply_stock_change

from .models import Receipt, TransactionItem


def _deduct(material, qty, user, reason="") -> None:
    """Deduct stock, routing roll-materials through FIFO area consumption."""
    if qty <= 0:
        return
    if material.is_roll_material:
        consume_area(material, qty, user=user, reason=reason)
    else:
        apply_stock_change(material, -qty, user=user)


def _restore(material, qty, user, reason="") -> None:
    if qty <= 0:
        return
    if material.is_roll_material:
        restore_area(material, qty, user=user, reason=reason)
    else:
        apply_stock_change(
            material, qty, log_type=InventoryLog.Type.ADJUSTMENT, reason=reason, user=user
        )


def _deduct_stock_for_item(item: TransactionItem, user, *, restore=False) -> None:
    """Deduct (or restore) stock for a single line item.

    Cutting now produces two separate lines (a MATERIAL line for the cut material
    and a SERVICE line for the master's work), so the MATERIAL line handles its
    own area; service lines only consume their recipe (technological-card) extras.
    """
    from services.models import ServiceRecipe

    fn = _restore if restore else _deduct
    if item.type == TransactionItem.Type.MATERIAL and item.material_id:
        # Whole-piece sales deduct the piece area; area/qty sales deduct quantity.
        qty = item.quantity
        if item.sale_mode == TransactionItem.SaleMode.PIECE and item.material.piece_area:
            qty = item.material.piece_area * item.quantity
        fn(item.material, qty, user)
        return
    if item.type != TransactionItem.Type.SERVICE or not item.service_id:
        return

    # Extra recipe materials (e.g. fasteners for installation, glue, …).
    for recipe in item.service.recipes.select_related("material").all():
        if recipe.consumption_mode == ServiceRecipe.Mode.PER_SQM:
            consumed = recipe.consumption_per_unit * item.quantity
        else:  # FIXED per order
            consumed = recipe.consumption_per_unit
        fn(recipe.material, consumed, user)


def _build_item(receipt, entry) -> list[TransactionItem]:
    """Create the TransactionItem(s) for one checkout entry, pricing each correctly.

    Returns a LIST because cutting expands into two lines (material + work):
    - MATERIAL: by piece (price=piece_price, qty=count) or by area (price=price_per_sqm,
      qty=area from width×length or given quantity).
    - SERVICE / CUTTING: a SERVICE line for the master's work (area × rate_flat) PLUS,
      if a material was chosen, a MATERIAL line for the cut material (area × price_per_sqm).
    - SERVICE / INTERIOR install: area × rate_flat (no separate material line).
    - SERVICE / EXTERIOR install: per piece (rate_per_piece × count).
    - SERVICE / FIXED (installation, other): base_price × count.
    """
    def _override(key):
        v = entry.get(key)
        return Decimal(str(v)) if v not in (None, "") else None

    def _priced(key, default):
        """Honour an explicit price/rate override — including 0 (бесплатно) —
        falling back to ``default`` only when the override is absent. A plain
        ``override or default`` would discard Decimal('0') as falsy."""
        v = _override(key)
        return v if v is not None else default

    item_type = entry["type"]

    if item_type == TransactionItem.Type.MATERIAL:
        material = entry["material"]
        mode = entry.get("mode") or TransactionItem.SaleMode.SQM
        qty = Decimal(entry.get("quantity") or 0)
        if mode == TransactionItem.SaleMode.PIECE:
            # Опт: при заказе от wholesale_min_qty листов цена за лист сама
            # переключается на оптовую (если её задал админ). Ручной override
            # цены (если есть) всегда в приоритете.
            price = _priced("material_price", material.piece_price_for_qty(qty))
        else:
            mode = TransactionItem.SaleMode.SQM
            price = _priced(
                "material_price",
                material.sqm_price if material.is_roll_material else material.price_per_unit,
            )
        return [TransactionItem.objects.create(
            receipt=receipt, type=item_type, material=material,
            quantity=qty, price_per_item=price,
            sale_mode=mode,
        )]

    service = entry["service"]

    # Area-priced services: cutting and interior install. Work is computed
    # automatically from the cut area (width × length); no manual entry.
    if service.uses_area:
        width = entry.get("width")
        length = entry.get("length")
        area = Decimal(str(width)) * Decimal(str(length)) if width and length else Decimal(entry.get("quantity") or 0)
        material = entry.get("material")

        # Cutting → the material's own cut rate; interior install → service rate.
        # (Both per кв.м; an admin may override the rate at sale time.)
        if service.uses_running_meter:
            rate = _priced("cut_rate", material.cut_rate_per_pm if material else Decimal("0"))
        else:
            rate = _priced("cut_rate", service.rate_flat)
        # Резку считаем по погонному метру (длине реза): можно ввести вручную,
        # иначе берём площадь куска. Материал всегда списывается/считается по площади.
        work_qty = area
        if service.uses_running_meter:
            rm = entry.get("running_meters")
            if rm not in (None, ""):
                work_qty = Decimal(str(rm))
        work = TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.SERVICE, service=service,
            quantity=work_qty, price_per_item=rate,
            width=Decimal(str(width)) if width else None,
            length=Decimal(str(length)) if length else None,
        )
        items = [work]
        # The cut/used material is billed as its own line (area × per-кв.м price,
        # or a manual per-кв.м override entered at sale time). Cutting work on a
        # whole sheet has no cut dimensions (area=0) — the sheet is billed
        # separately as a PIECE line, so we bill only the work here.
        if service.uses_material and material and area > 0:
            items.append(TransactionItem.objects.create(
                receipt=receipt, type=TransactionItem.Type.MATERIAL, material=material,
                quantity=area, price_per_item=_priced("material_price", material.sqm_price),
                sale_mode=TransactionItem.SaleMode.SQM,
            ))
        return items

    # Per-piece service: exterior install (price per letter × count).
    if service.uses_pieces:
        return [TransactionItem.objects.create(
            receipt=receipt, type=item_type, service=service,
            quantity=Decimal(entry.get("quantity") or 1), price_per_item=service.rate_per_piece,
        )]

    # FIXED-price service (legacy installation / other)
    return [TransactionItem.objects.create(
        receipt=receipt, type=item_type, service=service,
        quantity=Decimal(entry.get("quantity") or 1), price_per_item=service.base_price,
    )]


@transaction.atomic
def create_sale(*, client, cashier, payment_method, items_data, amount_paid=None) -> Receipt:
    """Create a receipt with its line items.

    Cash sales are settled immediately (PAID + stock deducted). Online sales are
    created PENDING; stock is deducted only once payment is confirmed
    (see ``confirm_payment``).
    """
    receipt = Receipt.objects.create(
        client=client,
        cashier=cashier,
        payment_method=payment_method,
        payment_status=Receipt.PaymentStatus.PENDING,
    )

    for entry in items_data:
        _build_item(receipt, entry)  # creates one or more line items

    total = receipt.recalculate_total()

    if payment_method != Receipt.PaymentMethod.ONLINE:
        # Наличные / MBank / DemirBank — оплата принимается на месте (перевод/нал):
        # деньги получены, склад списывается сразу, предоплата опциональна, а
        # остаток (если платят частично) остаётся долгом (чек — PENDING).
        paid = total if amount_paid is None else min(max(Decimal(str(amount_paid)), Decimal("0")), total)
        _deduct_all(receipt)
        receipt.stock_deducted = True
        receipt.amount_paid = paid
        receipt.payment_status = (
            Receipt.PaymentStatus.PAID if paid >= total else Receipt.PaymentStatus.PENDING
        )
    else:
        _create_online_invoice(receipt)

    receipt.save()
    return receipt


def _deduct_all(receipt: Receipt) -> None:
    """Deduct stock for every line item of the receipt."""
    for item in receipt.items.all():
        _deduct_stock_for_item(item, receipt.cashier)


def _settle(receipt: Receipt) -> None:
    """Mark fully paid, deducting stock once if not already done."""
    if not receipt.stock_deducted:
        _deduct_all(receipt)
        receipt.stock_deducted = True
    receipt.amount_paid = receipt.total_price
    receipt.payment_status = Receipt.PaymentStatus.PAID


def _create_online_invoice(receipt: Receipt) -> None:
    from integrations.payments import get_gateway

    invoice = get_gateway().create_invoice(receipt)
    receipt.payment_reference = invoice.reference
    receipt.payment_url = invoice.payment_url
    receipt.payment_status = Receipt.PaymentStatus.PENDING


class OrderClosed(Exception):
    pass


@transaction.atomic
def add_items_to_receipt(receipt: Receipt, items_data, *, user=None):
    """Append items to an existing order (дозаказ — e.g. installation added later).

    New items are priced/built like a normal sale. If the receipt was already
    settled (PAID), the new items' stock is deducted immediately (the surcharge
    is collected on the spot); for a still-PENDING online order they are simply
    added and deducted when payment is confirmed. Returns (receipt, surcharge).
    """
    if receipt.status == Receipt.Status.CANCELLED or receipt.payment_status == Receipt.PaymentStatus.REFUNDED:
        raise OrderClosed("Чек закрыт или возвращён — добавление невозможно.")

    settled = receipt.payment_status in (
        Receipt.PaymentStatus.PAID,
        Receipt.PaymentStatus.PARTIALLY_REFUNDED,
    )
    surcharge = Decimal("0")
    for entry in items_data:
        for item in _build_item(receipt, entry):
            surcharge += item.line_total
            if settled:
                _deduct_stock_for_item(item, user)

    receipt.recalculate_total()
    receipt.save(update_fields=["total_price", "updated_at"])
    return receipt, surcharge


@transaction.atomic
def confirm_payment(receipt: Receipt) -> Receipt:
    """Called when the payment gateway confirms an online payment."""
    if receipt.payment_status == Receipt.PaymentStatus.PAID:
        return receipt
    _settle(receipt)
    receipt.save(update_fields=["payment_status", "amount_paid", "stock_deducted", "updated_at"])
    return receipt


@transaction.atomic
def refund_receipt(receipt: Receipt, *, item_ids=None, user=None) -> Receipt:
    """Refund the whole receipt or specific line items, returning stock.

    Returns deducted materials back to the warehouse and updates statuses.
    """
    items = receipt.items.filter(is_returned=False)
    if item_ids:
        items = items.filter(id__in=item_ids)

    # Stock was only deducted if the receipt was actually settled.
    stock_was_deducted = receipt.stock_deducted or receipt.payment_status in (
        Receipt.PaymentStatus.PAID,
        Receipt.PaymentStatus.PARTIALLY_REFUNDED,
    )

    refunded_total = Decimal("0")
    for item in items:
        # Restore stock AND book the refund only for a settled sale. An unpaid
        # order (e.g. a pending online invoice) never deducted stock and collected
        # no money, so refunding it restores nothing and books 0 — not a phantom
        # refund of money the customer never paid. For settled sales the refund is
        # the returned line's value, which keeps the debt formula consistent:
        # (total_price − refunded_amount) stays equal to the value of kept lines.
        if stock_was_deducted:
            _deduct_stock_for_item(item, user, restore=True)
            refunded_total += item.quantity * item.price_per_item
        item.is_returned = True
        item.save(update_fields=["is_returned"])

    receipt.refunded_amount += refunded_total
    remaining = receipt.items.filter(is_returned=False).exists()
    if remaining:
        receipt.payment_status = Receipt.PaymentStatus.PARTIALLY_REFUNDED
    else:
        receipt.payment_status = Receipt.PaymentStatus.REFUNDED
        receipt.status = Receipt.Status.CANCELLED
    receipt.save(update_fields=["refunded_amount", "payment_status", "status", "updated_at"])
    return receipt
