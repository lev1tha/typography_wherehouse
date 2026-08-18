"""Roll (lot) intake and FIFO area consumption for roll-materials.

Roll materials are stocked and sold by area (кв.м). Each received roll keeps
its own cost and markup; the material's retail price-per-кв.м tracks the most
recent roll. Sales consume area oldest-roll-first (FIFO).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from .models import InventoryLog, Material, Roll



def _som(value) -> str:
    """Сумма целыми сомами с разрядами через пробел: 12000 → «12 000»."""
    return f"{int(Decimal(str(value or 0)).quantize(Decimal('1'))):,}".replace(",", " ")

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
    declared_length=None,
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
        # Заявленная поставщиком длина — рядом с принятой. Без этой пары
        # систематический недолив не виден ни в одном отчёте.
        declared_length=declared_length,
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
        # Площадь до двух знаков и сумма с разрядами: «(14.88 кв.м), 12 000 сом»
        # читается, «(14.8840 кв.м), 12000.00 сом» — нет.
        reason=(
            f"Поступление: {roll.dimensions_label} "
            f"({Decimal(area).quantize(Decimal('0.01'))} кв.м), "
            f"{_som(purchase_cost)} сом"
        ),
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
    rolls = list(
        Roll.objects.select_for_update()
        .filter(material=locked, remaining_area__gt=0)
        .order_by("received_at")
    )

    for roll in rolls:
        if remaining <= 0:
            break
        take = min(roll.remaining_area, remaining)
        roll.remaining_area -= take
        roll.save(update_fields=["remaining_area"])
        cogs += take * roll.cost_per_sqm
        remaining -= take

    # Партии кончились, а остаток по материалу ещё есть. Это НЕ поломка: остаток
    # правит инвентаризация, партий при этом не создавая, и материал, лежавший на
    # полке до системы, тоже приходит без партии. Но раньше цикл на этом просто
    # заканчивался, и весь такой кусок уходил с НУЛЕВОЙ себестоимостью — маржа и
    # прибыль по заказу оказывались завышены, причём молча.
    #
    # Оцениваем его последней закупочной ценой — ровно так же, как оценивает тот
    # же «хвост» `Material.stock_value`: другой цены у него нет, и две цифры об
    # одном материале должны считаться одинаково.
    if remaining > 0:
        cogs += remaining * (locked.purchase_price or Decimal("0"))

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
def metres_available(material: Material) -> Decimal:
    """Сколько погонных метров лежит на складе — суммой по рулонам."""
    total = Decimal("0")
    for roll in Roll.objects.filter(material=material, remaining_area__gt=0):
        if roll.width:
            total += roll.remaining_area / roll.width
    return total


@transaction.atomic
def consume_metres(
    material: Material,
    metres: Decimal,
    *,
    user=None,
    reason: str = "",
    log_type: str | None = None,
    receipt=None,
    happened_at=None,
    preferred_roll=None,
) -> Decimal:
    """Списать `metres` погонных метров, идя по рулонам FIFO.

    `preferred_roll` — рулон, с которого мастер решил начать. Он встаёт первым,
    остальные идут за ним обычным порядком: если в выбранном не хватило, режем
    дальше со следующего — так и происходит в цехе, когда рулон кончается
    посреди заказа. Не указан — обычный FIFO, то есть початый уходит первым.

    Метры НЕЛЬЗЯ один раз перевести в площадь по ширине из карточки: у каждого
    рулона своя замороженная ширина, и 1.4 м оракала шириной 1.0 — это совсем
    другая площадь и другая себестоимость, чем 1.4 м шириной 1.52. Поэтому идём
    по рулонам и у каждого переводим метры в площадь ЕГО шириной.

    Со склада уходит вся ширина полотна: отрезают поперёк рулона целиком, узкая
    полоса остаётся обрезком цеха. Возвращает себестоимость списанного.
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    need = Decimal(metres)
    if need <= 0:
        return Decimal("0")

    rolls = list(
        Roll.objects.select_for_update()
        .filter(material=locked, remaining_area__gt=0)
        .order_by("received_at")
    )
    if preferred_roll is not None:
        # Выбранный рулон встаёт первым, остальные — обычным порядком.
        pk = getattr(preferred_roll, "pk", preferred_roll)
        chosen = next((r for r in rolls if r.pk == pk), None)
        if chosen is not None:
            rolls = [chosen] + [r for r in rolls if r.pk != chosen.pk]
    have = sum((r.remaining_area / r.width for r in rolls if r.width), Decimal("0"))
    if have < need:
        raise InsufficientStock(
            f"Недостаточно «{locked.name}»: нужно {need} пог.м, "
            f"в наличии {have.quantize(Decimal('0.01'))}."
        )

    was_above = locked.quantity > locked.critical_balance
    cogs = Decimal("0")
    area_taken = Decimal("0")
    remaining = need
    for roll in rolls:
        if remaining <= 0:
            break
        if not roll.width:
            continue
        roll_metres = roll.remaining_area / roll.width
        take_m = min(roll_metres, remaining)
        take_area = take_m * roll.width
        roll.remaining_area -= take_area
        roll.save(update_fields=["remaining_area"])
        cogs += take_area * roll.cost_per_sqm
        area_taken += take_area
        remaining -= take_m

    locked.quantity -= area_taken
    locked.save(update_fields=["quantity", "updated_at"])

    if log_type:
        entry = InventoryLog(
            type=log_type,
            material=locked,
            quantity_changed=-area_taken,
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
    material.refresh_from_db()
    return cogs


@transaction.atomic
def restore_metres(
    material: Material,
    metres: Decimal,
    *,
    user=None,
    reason: str = "",
    log_type: str | None = None,
    receipt=None,
    happened_at=None,
    preferred_roll=None,
) -> None:
    """Вернуть `metres` погонных метров — зеркало `consume_metres`.

    Доливаем рулоны от старых к новым, каждый не выше его исходной площади, и
    переводим метры в площадь ЕГО шириной: рулон должен вернуться ровно в то
    состояние, из которого его резали.
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    add = Decimal(metres)
    if add <= 0:
        return
    rolls = list(
        Roll.objects.select_for_update().filter(material=locked).order_by("received_at")
    )
    if preferred_roll is not None:
        # Выбранный рулон встаёт первым, остальные — обычным порядком.
        pk = getattr(preferred_roll, "pk", preferred_roll)
        chosen = next((r for r in rolls if r.pk == pk), None)
        if chosen is not None:
            rolls = [chosen] + [r for r in rolls if r.pk != chosen.pk]
    remaining = add
    area_added = Decimal("0")
    for roll in rolls:
        if remaining <= 0:
            break
        if not roll.width:
            continue
        headroom_area = roll.initial_area - roll.remaining_area
        if headroom_area <= 0:
            continue
        give_m = min(headroom_area / roll.width, remaining)
        give_area = give_m * roll.width
        roll.remaining_area += give_area
        roll.save(update_fields=["remaining_area"])
        area_added += give_area
        remaining -= give_m
    # Излишек (вернули больше, чем резали) кладём на самый свежий рулон, чтобы
    # остаток материала не разошёлся с суммой рулонов.
    if remaining > 0:
        target = next((r for r in reversed(rolls) if r.width), None)
        if target:
            extra_area = remaining * target.width
            target.remaining_area += extra_area
            target.save(update_fields=["remaining_area"])
            area_added += extra_area

    locked.quantity += area_added
    locked.save(update_fields=["quantity", "updated_at"])
    if log_type:
        entry = InventoryLog(
            type=log_type,
            material=locked,
            quantity_changed=area_added,
            reason=reason,
            receipt=receipt,
            created_by=user,
        )
        if happened_at:
            entry.happened_at = happened_at
        entry.save()
    material.refresh_from_db()


@transaction.atomic
def stocktake_roll(roll: Roll, counted_metres: Decimal, *, reason_code, note="", user=None):
    """Промер рулона рулеткой: привести остаток к факту и записать АКТ.

    Правкой остатка это делать нельзя: она приводит число к факту и на этом
    заканчивается — расхождение исчезает вместе с причиной. Здесь остаток
    правится и объяснение остаётся: сколько было по системе, сколько намерили,
    на сколько разошлось и почему.

    Возвращает созданный акт.
    """
    from .models import RollStocktake

    locked_roll = Roll.objects.select_for_update().get(pk=roll.pk)
    material = Material.objects.select_for_update().get(pk=locked_roll.material_id)
    if not locked_roll.width:
        raise InsufficientStock(
            "У этой партии не задана ширина — промерить её в метрах нельзя."
        )

    expected = (locked_roll.remaining_area / locked_roll.width).quantize(Decimal("0.01"))
    counted = Decimal(counted_metres)
    # Больше, чем пришло, в рулоне быть не может: это уже не промер, а ошибка
    # ввода, и молча раздувать партию нельзя — на неё завязана себестоимость.
    max_metres = (locked_roll.initial_area / locked_roll.width).quantize(Decimal("0.01"))
    if counted > max_metres:
        raise InsufficientStock(
            f"В рулоне не может быть больше {max_metres} м — столько его и приняли."
        )

    new_area = (counted * locked_roll.width).quantize(Decimal("0.0001"))
    delta_area = new_area - locked_roll.remaining_area

    locked_roll.remaining_area = new_area
    locked_roll.save(update_fields=["remaining_area"])
    material.quantity = (material.quantity or Decimal("0")) + delta_area
    material.save(update_fields=["quantity", "updated_at"])

    act = RollStocktake.objects.create(
        roll=locked_roll,
        expected_metres=expected,
        counted_metres=counted,
        difference=(counted - expected),
        reason_code=reason_code,
        note=(note or "").strip()[:255],
        created_by=user,
    )
    # В журнале склада промер тоже виден движением — иначе остаток меняется, а
    # в ленте движений пусто, и склад перестаёт сходиться сам с собой.
    if delta_area:
        InventoryLog.objects.create(
            type=InventoryLog.Type.ADJUSTMENT,
            material=material,
            quantity_changed=delta_area,
            reason=(
                f"Промер рулона {locked_roll.code or f'№{locked_roll.pk}'}: "
                f"было {expected} м, намерено {counted} м "
                f"({act.get_reason_code_display()})"
            ),
            created_by=user,
        )
    return act


@transaction.atomic
def write_off_roll(roll: Roll, metres: Decimal, *, reason: str = "", user=None) -> Decimal:
    """Списать `metres` погонных метров С ЭТОГО рулона — порча, брак, утеря.

    Списание рулонного материала общим числом в кв.м (`consume_area`) шло FIFO
    со СТАРЕЙШЕГО рулона: «порвали 2 м рулона №8» вводили как «2» — это 2 кв.м,
    то есть 1.67 м, — и они уходили с рулона №7 (целого) и лишь остаток с №8.
    После этого остаток обоих рулонов врал, а себестоимость списания бралась
    не от того рулона (171 сом вместо 400). Брак случается с конкретным рулоном
    на полке и меряется рулеткой — значит, и списывается по рулону, в метрах,
    его шириной и по его цене. Возвращает списанную площадь, кв.м.
    """
    locked_roll = Roll.objects.select_for_update().get(pk=roll.pk)
    material = Material.objects.select_for_update().get(pk=locked_roll.material_id)
    label = locked_roll.code or f"№{locked_roll.pk}"
    if not locked_roll.width:
        raise InsufficientStock(
            f"У партии {label} не задана ширина — списать её в метрах нельзя."
        )
    metres = Decimal(str(metres))
    if metres <= 0:
        raise InsufficientStock("Укажите, сколько метров списать.")
    have = locked_roll.metres_remaining
    if metres > have:
        raise InsufficientStock(
            f"В рулоне {label} только {have} м — {metres} м списать нельзя."
        )
    area = (metres * locked_roll.width).quantize(Decimal("0.0001"))
    # Хвост округления: последние метры рулона списываем до нуля, а не до 0.0004.
    if metres == have or area > locked_roll.remaining_area:
        area = locked_roll.remaining_area

    was_above = material.quantity > material.critical_balance
    locked_roll.remaining_area -= area
    locked_roll.save(update_fields=["remaining_area"])
    material.quantity = (material.quantity or Decimal("0")) - area
    material.save(update_fields=["quantity", "updated_at"])
    InventoryLog.objects.create(
        type=InventoryLog.Type.WRITE_OFF,
        material=material,
        quantity_changed=-area,
        reason=f"{reason} Рулон {label}: {metres.normalize():f} м".strip(),
        created_by=user,
    )
    if was_above and material.quantity <= material.critical_balance:
        from integrations.telegram import notify_low_stock

        notify_low_stock(material)
    roll.refresh_from_db()
    return area


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


def lots_area(material: Material) -> Decimal:
    """Сумма остатков всех партий материала, кв.м — то, что реально лежит по
    рулонам. `Material.quantity` обязан с ней сходиться; когда не сходится,
    разница — «хвост сверх партий», см. `reconcile_with_lots`."""
    return sum(
        (r.remaining_area for r in Roll.objects.filter(material=material)),
        Decimal("0"),
    )


class NothingToReconcile(Exception):
    pass


@transaction.atomic
def reconcile_with_lots(material: Material, *, user=None) -> Decimal:
    """Свести `Material.quantity` рулонного материала с суммой его партий.

    У материала, который продаётся метрами, партия — единственный носитель
    правды: режут из рулона, промеряют рулон, возвращают в рулон. Число в
    карточке — производное. Разойтись они могут только помимо этих путей
    (материал переключили на «рулон» уже с остатком без партий, старая правка
    остатка «в кв.м» подняла число, не создав партии) — и такой хвост не
    списать ни продажей, ни промером: он не принадлежит ни одному рулону и
    висит в остатке и в стоимости склада вечно.

    Общая инвентаризация в кв.м здесь не подходит: она гонит расхождение через
    FIFO, то есть режет СТАРЕЙШИЙ РУЛОН, а хвост оставляет как был. Поэтому
    сведение — отдельная операция: число приводится к сумме партий, разница
    уходит строкой в журнал склада. Возвращает дельту (сколько добавили или
    сняли с остатка).
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    target = lots_area(locked)
    delta = target - (locked.quantity or Decimal("0"))
    if delta == 0:
        raise NothingToReconcile(
            f"«{locked.name}»: остаток сходится с рулонами ({target} кв.м) — сводить нечего."
        )
    locked.quantity = target
    locked.save(update_fields=["quantity", "updated_at"])
    InventoryLog.objects.create(
        type=InventoryLog.Type.ADJUSTMENT,
        material=locked,
        quantity_changed=delta,
        reason=(
            f"Сведение остатка с рулонами: было {target - delta} кв.м, "
            f"по рулонам {target} кв.м"
        ),
        created_by=user,
    )
    material.refresh_from_db()
    return delta
