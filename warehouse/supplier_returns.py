"""«Вернуть поставщику» (G1-N2): товар уехал обратно, накладная стала меньше.

В Excel это строка с минусом. В системе обходились «Исправить приход» (склад
уменьшался) и ручной записью в кассе (деньги возвращались) — без связи между
собой, без следа «вернули поставщику».

Что делает возврат (одной транзакцией, только администратор):

* СКЛАД: партия накладной уменьшается на возвращённое количество — по ЦЕНЕ
  ПАРТИИ, поэтому потерь в ОПиУ нет (это не брак и не недостача), а цена
  единицы остальных штук не меняется. Больше, чем осталось на полке, вернуть
  нельзя: проданное уже ушло к клиентам.
* НАКЛАДНАЯ — как в бумаге, возврат ДАТОЙ ВОЗВРАТА, как строка с минусом в
  Excel (D-171 для закрытого месяца; с S3 перепроверки, RU-N23, — для любого):
  накладная, её строка, партия (принято и закуп) и приход в журнале остаются
  как были; закуп дня возврата меньше на стоимость возвращённого, склад
  уменьшается записью «Возврат поставщику» с минусом и стоимостью по партии
  (по её цене, без потерь), долг по накладной — на ту же сумму
  (`Supply.returned_after`). Раньше у накладной открытого месяца возврат
  переписывал запись прихода в «Движении» («после возврата поставщику») и
  саму накладную (D-113, `in_place=True` — такие возвраты в базе остаются и
  читаются как раньше).
* ДЕНЬГИ: либо возвращены на счёт (платёж-строка вида «возврат денег»,
  приход в кассу), либо остались кредитом у поставщика — накладная оплачена
  больше своей суммы, и сальдо поставщика это показывает.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

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


def _closed(day) -> bool:
    from finance.periods import is_closed

    return bool(day and is_closed(day))


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
    left = min(left, line.quantity - _returned_after(line)[0])
    units = left / per_unit if per_unit else left
    return unit, units.quantize(CENT, rounding="ROUND_DOWN"), Decimal(total_units)


def _returned_after(line: SupplyLine):
    """(площадь, сом, валюта) строки, уже возвращённые ДАТОЙ ВОЗВРАТА (строка
    накладной закрытого месяца при этом не уменьшалась). Через возвраты
    накладной: в списке они предзагружены (`returns__lines`), лишних запросов
    на строку нет."""
    area = cost = fc = ZERO
    for ret in line.supply.returns.all():
        if ret.in_place:
            continue
        for r in ret.lines.all():
            if r.supply_line_id == line.pk:
                area += r.area
                cost += r.cost
                fc += r.cost_fc or ZERO
    return area, cost, fc


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
    if returned_on < supply.received_on:
        raise SupplyError("Дата возврата не может быть раньше даты накладной.")
    ensure_open(returned_on, "Оформить возврат поставщику этой датой")
    # Накладная не переписывается ни в каком месяце: возврат — строка с минусом
    # датой возврата (D-171; для открытого месяца — с RU-N23).
    in_place = False
    if mode not in ("CREDIT", "REFUND"):
        raise SupplyError("Укажите, что с деньгами: вернуть на счёт или оставить кредитом у поставщика.")
    if not items:
        raise SupplyError("Укажите, что возвращаем: хотя бы одна строка с количеством.")

    total_before, paid_total = supply.total_cost, supply.paid_total
    label = supply.number or f"#{supply.pk}"
    ret = SupplierReturn.objects.create(
        supply=supply, supplier=supply.supplier, returned_on=returned_on,
        amount=ZERO, note=note[:255], created_by=user, in_place=in_place,
    )
    returned_total = ZERO
    returned_fc = ZERO
    seen = set()
    for item in items:
        line_id = getattr(item.get("line"), "pk", item.get("line"))
        if line_id in seen:
            raise SupplyError("Одна и та же строка накладной указана дважды.")
        seen.add(line_id)
        try:
            line = SupplyLine.objects.select_for_update(of=("self",)).select_related("material", "roll").get(
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
        value, area, text, value_fc = _return_line_after(supply, line, qty, label, user, returned_on)
        returned_total += value
        returned_fc += value_fc or ZERO
        SupplierReturnLine.objects.create(
            ret=ret, supply_line=line, material=line.material, label=text[:160],
            quantity=qty, area=area, cost=value, cost_fc=value_fc,
        )

    ret.amount = returned_total
    if supply.is_foreign:
        ret.amount_fc = returned_fc
    # Сохраняем сумму до денег: у возврата датой возврата долг и переплату
    # накладной считает именно она (`Supply.returned_after`).
    ret.save(update_fields=["amount", "amount_fc"])
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
def _return_line_after(supply, line, qty, label, user, returned_on):
    """Возврат по строке накладной — датой возврата (D-171; с RU-N23 — и у
    накладной открытого месяца).

    Накладная, её строка, партия (принято и закуп) и приход в журнале не
    меняются: приход — как в бумаге. Уменьшается остаток партии — записью
    журнала «Возврат поставщику» с минусом и стоимостью датой возврата, со своей
    раскладкой по партии, — и закуп дня возврата (`purchases_from_stock_by_day`
    вычитает такие возвраты). Стоимость — доля строки: цена единицы остальных
    не меняется, потерь в ОПиУ нет. Если партия — ровно эта строка, доля
    считается так же, как продажа (на сколько подешевела полка партии, STK-10):
    закуп минус возврат сходится со стоимостью склада до тыйына."""
    from .rolls import record_lot_moves
    from .waste import _moment

    material = Material.objects.select_for_update().get(pk=line.material_id)
    roll = Roll.objects.select_for_update().get(pk=line.roll_id) if line.roll_id else None
    unit, per_unit, _total_units = line_unit(line)
    area = _q(qty * per_unit, AREA) if per_unit else _q(qty, AREA)
    done_area, done_cost, done_fc = _returned_after(line)
    left_area = line.quantity - done_area

    if roll is not None:
        if area > roll.remaining_area + TINY:
            on_shelf = roll.remaining_area / per_unit if per_unit else roll.remaining_area
            raise SupplyError(
                f"«{material.name}»: на полке из этой поставки осталось {_num(_q(on_shelf))} {unit}, "
                f"вернуть {_num(qty)} нельзя — остальное уже продано или списано."
            )
        area = min(area, roll.remaining_area)
    else:
        in_lots = sum((r.remaining_area for r in material.rolls.all()), ZERO)
        loose = (material.quantity or ZERO) - in_lots
        if area > loose + TINY:
            raise SupplyError(
                f"«{material.name}»: на складе вне партий {_num(_q(max(loose, ZERO)))} {unit}, "
                f"вернуть {_num(qty)} нельзя — часть уже продана или списана."
            )
    if area > left_area + TINY:
        raise SupplyError(
            f"«{material.name}»: по накладной принято {_num(line.quantity)}, "
            f"уже возвращено {_num(done_area)} — вернуть больше нельзя."
        )
    whole = area >= left_area - TINY
    same_lot = (
        roll is not None and roll.purchase_cost == line.cost and roll.initial_area == line.quantity
    )
    share = roll.take_cost(area) if same_lot else _q(line.cost * area / line.quantity)
    value = (line.cost - done_cost) if whole else min(share, line.cost - done_cost)
    value_fc = None
    if line.cost_fc is not None:
        value_fc = (line.cost_fc - done_fc) if whole else min(
            _q(line.cost_fc * area / line.quantity), line.cost_fc - done_fc,
        )

    if roll is not None:
        roll.remaining_area -= area
        roll.save(update_fields=["remaining_area"])
    material.quantity = (material.quantity or ZERO) - area
    material.save(update_fields=["quantity", "updated_at"])
    text = f"{material.name}: {_num(qty)} {unit} на {_som(value)} сом"
    entry = InventoryLog.objects.create(
        type=InventoryLog.Type.CORRECTION, material=material, quantity_changed=-area,
        actual_price=(roll.cost_per_sqm if roll is not None else line.unit_cost),
        cost=value, roll=roll, supply=supply, created_by=user,
        reason=(
            f"Возврат поставщику (накладная {label}"
            f"{' закрытого месяца' if _closed(supply.received_on) else ''}, "
            f"датой {returned_on:%d.%m.%Y}): {text}"
        ),
        happened_at=_moment(returned_on),
    )
    if roll is not None:
        record_lot_moves(entry, [(roll.pk, -area)])
    return value, area, text, value_fc
