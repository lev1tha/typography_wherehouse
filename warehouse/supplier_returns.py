"""«Вернуть поставщику» (G1-N2): товар уехал обратно, накладная стала меньше.

В Excel это строка с минусом. В системе обходились «Исправить приход» (склад
уменьшался) и ручной записью в кассе (деньги возвращались) — без связи между
собой, без следа «вернули поставщику».

Что делает возврат (одной транзакцией, только администратор):

* СКЛАД: партия накладной уменьшается на возвращённое количество — по ЦЕНЕ
  ПАРТИИ, поэтому потерь в ОПиУ нет (это не брак и не недостача), а цена
  единицы остальных штук не меняется. Больше, чем осталось на полке, вернуть
  нельзя: проданное уже ушло к клиентам.
* НАКЛАДНАЯ: строка (количество, сумма) уменьшается — закуп месяца накладной и
  её сумма падают на стоимость возвращённого, запись прихода в журнале склада
  правится на месте (как в «Исправить приход», D-71) и рядом пишется запись
  «Возврат поставщику». Накладная закрытого периода не правится (D-75).
* ДЕНЬГИ: либо возвращены на счёт (платёж-строка вида «возврат денег»,
  приход в кассу), либо остались кредитом у поставщика — накладная оплачена
  больше своей суммы, и сальдо поставщика это показывает.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .lot_correction import _supply_log_for_line, supply_log_for_roll
from .models import (
    InventoryLog,
    Material,
    Roll,
    SupplierPayment,
    SupplierReturn,
    SupplierReturnLine,
    Supply,
    SupplyLine,
)
from .supplies import SupplyError

CENT = Decimal("0.01")
AREA = Decimal("0.0001")
TINY = Decimal("0.0001")
ZERO = Decimal("0")


def _q(value, step=CENT) -> Decimal:
    return Decimal(value).quantize(step, rounding=ROUND_HALF_UP)


def _num(value) -> str:
    return format(Decimal(value).normalize(), "f")


def _som(value) -> str:
    v = _q(value)
    whole = v == v.to_integral_value()
    text = f"{int(v):,}" if whole else f"{v:,.2f}"
    return text.replace(",", " ")


def line_unit(line: SupplyLine):
    """(подпись единицы, площадь на одну единицу или None, сколько единиц было)
    для строки накладной — то, в чём человек считает возврат: листы, метры,
    штуки."""
    roll = line.roll
    material = line.material
    if roll is not None and roll.form == Roll.Form.SHEET and roll.width and roll.height and roll.sheet_count:
        return "лист", roll.width * roll.height, roll.sheet_count
    if roll is not None and roll.form == Roll.Form.ROLL and roll.width and roll.length:
        return "м", roll.width, roll.length
    if roll is not None and roll.form == Roll.Form.PIECE:
        return material.get_unit_display(), None, roll.initial_area
    unit = "кв.м" if material.is_roll_material else material.get_unit_display()
    return unit, None, line.quantity


def returnable(line: SupplyLine):
    """(единица, сколько единиц строки ещё лежит на полке и может уехать назад,
    сколько единиц было принято) — для формы «Вернуть поставщику»."""
    unit, per_unit, total_units = line_unit(line)
    roll = line.roll
    if roll is not None:
        left = roll.remaining_area
    else:
        material = line.material
        in_lots = sum((r.remaining_area for r in material.rolls.all()), ZERO)
        left = max((material.quantity or ZERO) - in_lots, ZERO)
    left = min(left, line.quantity)
    units = left / per_unit if per_unit else left
    return unit, units.quantize(CENT, rounding="ROUND_DOWN"), Decimal(total_units)


@transaction.atomic
def return_to_supplier(supply: Supply, items, *, returned_on=None, mode="CREDIT",
                       account="", refund_amount=None, note="", user=None) -> SupplierReturn:
    """items: [{"line": <SupplyLine или id>, "quantity": число в единицах строки}]."""
    from finance import cash
    from finance.periods import ensure_open

    supply = Supply.objects.select_for_update().get(pk=supply.pk)
    if supply.is_opening:
        raise SupplyError(
            "Накладная начальных остатков — это склад на дату переезда, поставщику его не "
            "возвращают. Количество и цену правит «Исправить приход»."
        )
    returned_on = returned_on or timezone.localdate()
    if returned_on > timezone.localdate():
        raise SupplyError("Дата возврата не может быть в будущем.")
    ensure_open(supply.received_on, "Вернуть поставщику товар из накладной закрытого периода")
    ensure_open(returned_on, "Оформить возврат поставщику этой датой")
    if mode not in ("CREDIT", "REFUND"):
        raise SupplyError("Укажите, что с деньгами: вернуть на счёт или оставить кредитом у поставщика.")
    if not items:
        raise SupplyError("Укажите, что возвращаем: хотя бы одна строка с количеством.")

    total_before, paid_total = supply.total_cost, supply.paid_total
    label = supply.number or f"#{supply.pk}"
    ret = SupplierReturn.objects.create(
        supply=supply, supplier=supply.supplier, returned_on=returned_on,
        amount=ZERO, note=note[:255], created_by=user,
    )
    returned_total = ZERO
    seen = set()
    for item in items:
        line_id = getattr(item.get("line"), "pk", item.get("line"))
        if line_id in seen:
            raise SupplyError("Одна и та же строка накладной указана дважды.")
        seen.add(line_id)
        try:
            line = SupplyLine.objects.select_for_update().select_related("material", "roll").get(
                pk=line_id, supply=supply,
            )
        except SupplyLine.DoesNotExist:
            raise SupplyError("Строка не из этой накладной.")
        try:
            qty = Decimal(str(item.get("quantity")))
        except Exception:  # noqa: BLE001
            raise SupplyError("Некорректное количество возврата.")
        if qty <= 0:
            raise SupplyError("Количество возврата должно быть больше нуля.")
        value, area, text = _return_line(supply, line, qty, label, user, returned_on)
        returned_total += value
        SupplierReturnLine.objects.create(
            ret=ret, supply_line=line, material=line.material, label=text[:160],
            quantity=qty, area=area, cost=value,
        )

    ret.amount = returned_total
    refund = ZERO
    if mode == "REFUND":
        supply = Supply.objects.get(pk=supply.pk)
        overpaid = supply.overpaid
        want = Decimal(str(refund_amount)) if refund_amount not in (None, "") else min(returned_total, overpaid)
        if account not in ("CASH", "BANK"):
            raise SupplyError("Укажите, на какой счёт вернули деньги: в кассу или на банк.")
        if want <= 0 or want > overpaid:
            raise SupplyError(
                f"По накладной было оплачено больше новой суммы на {_som(overpaid)} сом — "
                "вернуть деньгами можно не больше. Остальное — кредит у поставщика."
                if overpaid > 0 else
                "По накладной ничего лишнего не оплачено — вернуть деньги нечем, "
                "долг по ней просто уменьшился."
            )
        refund = _q(want)
        entry = cash.supplier_refund(
            refund, account, supply=supply, happened_on=returned_on,
            note=f"Возврат от поставщика по накладной {label}", user=user,
        )
        SupplierPayment.objects.create(
            supplier=supply.supplier, supply=supply, kind=SupplierPayment.Kind.REFUND,
            paid_on=returned_on, account=account, amount=refund,
            note=f"Возврат товара по накладной {label}"[:255], created_by=user, cash_entry=entry,
        )
        ret.refund, ret.refund_account = refund, account
    ret.save()
    supply = Supply.objects.get(pk=supply.pk)
    ret.summary = {
        "total_before": total_before, "total_after": supply.total_cost,
        "paid": paid_total, "debt_after": supply.debt, "overpaid_after": supply.overpaid,
    }
    return ret


@transaction.atomic
def _return_line(supply, line, qty, label, user, returned_on):
    """Вернуть qty (в единицах строки) по одной строке. → (стоимость, площадь/шт, текст)."""
    material = Material.objects.select_for_update().get(pk=line.material_id)
    roll = Roll.objects.select_for_update().get(pk=line.roll_id) if line.roll_id else None
    unit, per_unit, total_units = line_unit(line)
    area = _q(qty * per_unit, AREA) if per_unit else _q(qty, AREA)

    if roll is not None:
        if area > roll.remaining_area + TINY:
            on_shelf = roll.remaining_area / per_unit if per_unit else roll.remaining_area
            raise SupplyError(
                f"«{material.name}»: на полке из этой поставки осталось {_num(_q(on_shelf))} {unit}, "
                f"вернуть {_num(qty)} нельзя — остальное уже продано или списано."
            )
    else:
        in_lots = sum((r.remaining_area for r in material.rolls.all()), ZERO)
        loose = (material.quantity or ZERO) - in_lots
        if area > loose + TINY:
            raise SupplyError(
                f"«{material.name}»: на складе вне партий {_num(_q(max(loose, ZERO)))} {unit}, "
                f"вернуть {_num(qty)} нельзя — часть уже продана или списана."
            )
    if area > line.quantity + TINY:
        raise SupplyError(f"«{material.name}»: по накладной принято {_num(line.quantity)}, вернуть больше нельзя.")

    whole = area >= line.quantity - TINY
    unit_price = (roll.cost_per_sqm if roll is not None else line.unit_cost)
    # Стоимость — ПРОПОРЦИОНАЛЬНО доле партии, а не «площадь × округлённая цена
    # кв.м»: так один лист из трёх на 25 200 стоит ровно 8 400, а цена единицы
    # остальных не уходит на копейку.
    value = line.cost if whole else min(_q(line.cost * area / line.quantity), line.cost)
    cost_fc = line.cost_fc
    new_fc = None
    if cost_fc is not None:
        new_fc = ZERO if whole else max(cost_fc - _q(cost_fc * area / line.quantity), ZERO)

    log = supply_log_for_roll(roll) if roll is not None else _supply_log_for_line(line)
    if log is None:
        raise SupplyError(
            "Запись прихода в журнале склада однозначно не найдена (двойной ввод?) — "
            "вернуть автоматически нельзя, иначе закуп разойдётся со складом."
        )

    new_area = line.quantity - area
    # --- партия ---
    if roll is not None:
        roll.initial_area -= area
        roll.remaining_area -= area
        roll.purchase_cost -= value
        if per_unit and roll.form == Roll.Form.SHEET:
            roll.sheet_count = (roll.sheet_count or ZERO) - qty
        elif per_unit and roll.form == Roll.Form.ROLL:
            roll.length = (roll.length or ZERO) - qty
        elif roll.form == Roll.Form.PIECE:
            roll.sheet_count = roll.initial_area
        roll.save()
        if roll.supplier_debt:
            roll.supplier_debt = max(roll.supplier_debt - value, ZERO)
            roll.save(update_fields=["supplier_debt"])
    # --- строка ---
    line.quantity = new_area
    line.cost = line.cost - value
    if new_fc is not None:
        line.cost_fc = new_fc
    if roll is not None:
        line.width, line.height = roll.width, roll.height
        line.length, line.sheet_count = roll.length, roll.sheet_count
    line.save()
    # --- материал ---
    material.quantity = (material.quantity or ZERO) - area
    material.save(update_fields=["quantity", "updated_at"])
    # --- журнал склада: приход на месте + запись «Возврат поставщику» ---
    log.quantity_changed = new_area
    if roll is not None and roll.form == Roll.Form.ROLL:
        log.metres_changed = roll.metres_initial
    log.reason = (
        f"Поступление: {roll.dimensions_label if roll is not None else _num(new_area) + ' ' + unit}, "
        f"{_som(line.cost)} сом (после возврата поставщику {returned_on:%d.%m.%Y})"
    )
    log.save(update_fields=["quantity_changed", "metres_changed", "reason"])
    text = f"{material.name}: {_num(qty)} {unit} на {_som(value)} сом"
    InventoryLog.objects.create(
        type=InventoryLog.Type.CORRECTION, material=material, quantity_changed=ZERO,
        actual_price=unit_price, roll=roll, supply=supply, created_by=user,
        reason=f"Возврат поставщику (накладная {label}): {text}",
    )
    return value, area, text
