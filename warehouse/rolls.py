"""Roll (lot) intake and FIFO area consumption for roll-materials.

Roll materials are stocked and sold by area (кв.м). Each received roll keeps
its own cost and markup; the material's retail price-per-кв.м tracks the most
recent roll. Sales consume area oldest-roll-first (FIFO).
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from .models import InventoryLog, InventoryLogLot, Material, Roll


def record_lot_moves(entry: InventoryLog | None, moves) -> None:
    """Записать, из каких партий ушла (−) или в какие вернулась (+) запись
    журнала `entry` — `[(партия или её pk, площадь со знаком)]`.

    По этим строкам считается остаток партии на прошлую дату (склад на дату,
    снимок, начало и конец периода в «Финансах»): сегодняшний остаток плюс
    всё, что ушло из партии после даты. Без них приходилось угадывать
    «с самых свежих», и сентябрь показывал 49 800 вместо 47 400.
    """
    if entry is None or entry.pk is None:
        return
    total: dict = {}
    for roll, area in moves or ():
        pk = getattr(roll, "pk", roll)
        if pk is None or not area:
            continue
        total[pk] = total.get(pk, Decimal("0")) + Decimal(area)
    rows = [InventoryLogLot(log=entry, roll_id=pk, area=area) for pk, area in total.items() if area]
    if rows:
        InventoryLogLot.objects.bulk_create(rows)


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
    if form == Roll.Form.PIECE:
        # Штучная партия: «площадь» — это количество штук (кг, литров), считать
        # из размеров нечего. Единица живёт в материале, не в партии.
        return Decimal(sheet_count).quantize(Decimal("0.0001"))
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
    production=None,
    paid_account=None,
    on_credit=False,
    site=None,
) -> Roll:
    """Receive a new lot (roll or sheets). Computes area from dimensions unless
    `area` is given directly; then creates the lot and refreshes material stock.

    ``received_at`` — дата поступления; поставки часто вносят задним числом.
    По ней же идёт FIFO, поэтому партия встаёт в очередь по своей настоящей
    дате, а не по моменту ввода.

    ``on_credit`` — при приёмке сказали «в долг»: вся стоимость партии встаёт
    долгом поставщику (`Roll.supplier_debt`), гасится потом `pay_lot_supplier`.
    """
    if area is None:
        area = compute_area(form, width=width, length=length, height=height, sheet_count=sheet_count)
    area = Decimal(area)
    locked = Material.objects.select_for_update().get(pk=material.pk)
    roll = Roll(
        material=locked,
        code=code,
        # Производство ПАРТИИ. Не указали — берём из карточки: обычно возят
        # оттуда же, и заставлять выбирать одно и то же на каждой приёмке
        # значит добавить ручного ввода там, где система знает ответ.
        production=production if production is not None else locked.production,
        # Площадка хранения (STK-05): где партия будет лежать. Пусто — не указана.
        site=site,
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
        supplier_debt=Decimal(purchase_cost) if on_credit else Decimal("0"),
        created_by=user,
    )
    if received_at:
        roll.received_at = received_at
    roll.save()
    # ШТУЧНАЯ партия материал не переделывает: у него своя единица (шт, кг, л)
    # и своя цена за неё. Пометить его площадным значило бы молча перевести
    # саморезы в квадратные метры — цены, остаток и вся касса поехали бы.
    if form != Roll.Form.PIECE:
        # The material is a roll-material; stock is the sum of remaining areas.
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
        # Рулон пришёл длиной — её и записываем: в журнале он должен читаться
        # метрами, как его меряют в цехе.
        metres_changed=roll.metres_initial if roll.form == Roll.Form.ROLL else None,
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
        # Партия — чтобы «Исправить приход» находил свою запись наверняка, а
        # не по материалу и площади (две одинаковые поставки не различить).
        roll=roll,
    )
    if received_at:
        entry.happened_at = received_at
    entry.save()

    # ЗАПЛАТИЛИ ПОСТАВЩИКУ — расход по кассе. Только если при приёмке выбрали
    # счёт: взяли в долг (или зовут из накладной, где оплата своя, на весь
    # документ) — записи нет. До 19.09 закуп мимо кассы проходил всегда, и
    # остаток «сколько в ящике» не знал про 1 678 477 сом, ушедших поставщикам.
    if paid_account:
        from finance import cash

        cash.supplier_paid(
            roll.purchase_cost, paid_account,
            roll=roll,
            happened_on=(received_at.date() if received_at else None),
            note=f"{locked.name}: {roll.dimensions_label}",
            user=user,
        )
    return roll


class SupplierPaymentError(Exception):
    """Оплату поставщику провести нельзя — с человеческим объяснением."""


@transaction.atomic
def pay_lot_supplier(roll: Roll, amount, account, *, paid_on=None, user=None) -> Decimal:
    """Заплатить поставщику за партию, взятую в долг.

    Деньги уходят из кассы или со счёта (`account`) датой оплаты, долг партии
    уменьшается. Больше долга не платим: переплату поставщику система не
    ведёт, а молча записанная она увела бы кассу ниже ящика.
    """
    from finance import cash

    locked = Roll.objects.select_for_update().get(pk=roll.pk)
    amount = Decimal(str(amount))
    if amount <= 0:
        raise SupplierPaymentError("Сумма должна быть больше нуля.")
    if account not in ("CASH", "BANK"):
        raise SupplierPaymentError("Укажите, чем платили: наличными или с банка.")
    if amount > locked.supplier_debt:
        raise SupplierPaymentError(
            f"По этой партии долг {locked.supplier_debt} — больше заплатить нельзя."
        )
    locked.supplier_debt -= amount
    locked.save(update_fields=["supplier_debt"])
    cash.supplier_paid(
        amount, account, roll=locked, happened_on=paid_on,
        note=f"Долг за {locked.material.name}: {locked.dimensions_label}", user=user,
    )
    return locked.supplier_debt


# Backwards-compatible alias.
def receive_roll(material, *, area, purchase_cost, code="", user=None):
    return receive_lot(
        material, form=Roll.Form.ROLL, area=area, purchase_cost=purchase_cost,
        code=code, user=user,
    )


class InsufficientStock(Exception):
    pass


# Хвост округления площади (XL-03/STK-08): остаток, который меньше сотки
# квадратного метра, — это не материал на полке, а след старого округления до
# сотых («6 листов» записывались как 17.86 вместо 17.8608). Списание последнего
# листа такой хвост забирает в ноль, а продажа последнего листа не спотыкается
# о недостающие 0.0008 кв.м.
TAIL_SQM = Decimal("0.01")


def kim_factor(material: Material, area) -> Decimal | None:
    """Доля КИМ (0 < к < 1), если раскрой этой площади её касается, иначе None.

    КИМ — у листового площадного материала (рулон режут на всю ширину, обрезок
    у него и так считается). Целые листы (площадь кратна площади листа)
    продаются как есть: лист целиком — это не раскрой. Пусто или 100 % — как
    раньше (STK-07/G4-N1).
    """
    kim = material.kim_percent
    if not kim or kim <= 0 or kim >= 100:
        return None
    if not material.is_roll_material or material.sells_by_metre:
        return None
    area = Decimal(str(area))
    if area <= 0:
        return None
    if material.piece_area and material.piece_area > 0 and area % material.piece_area == 0:
        return None
    return Decimal(kim) / Decimal("100")


def snap_tail(material: Material, qty) -> Decimal:
    """Количество к списанию с учётом хвоста округления.

    Только у площадного материала (кв.м): если после списания осталось бы
    0 < остаток ≤ 0.01 кв.м — списываем всё; если просят больше остатка не
    более чем на 0.01 кв.м — тоже всё, что есть. Иначе — как просили.
    """
    qty = Decimal(str(qty))
    if not material.is_roll_material:
        return qty
    have = material.quantity or Decimal("0")
    if have <= 0:
        return qty
    rest = have - qty
    if -TAIL_SQM <= rest <= TAIL_SQM and rest != 0:
        return have
    return qty


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
    preferred_roll=None,
    trace=None,
) -> Decimal:
    """Consume `area` кв.м from a roll-material, FIFO across rolls.

    ``trace`` — список, в который дописывается `(партия, площадь, None)` по
    каждой партии, из которой реально взяли. Строка чека запоминает по нему,
    откуда ушёл материал, и возврат кладёт его обратно туда же.

    Returns the total cost of goods consumed. Raises InsufficientStock if there
    is not enough remaining area across all rolls.

    ``preferred_roll`` — партия, с которой мастер решил начать; она встаёт
    первой, остальные идут за ней обычным порядком, и если в выбранной не
    хватило, добираем со следующей. Так это и происходит в цехе: пачка
    кончается посреди заказа. Не указана — обычный FIFO, старейшая первой.

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
    if (
        locked.quantity < need and locked.is_roll_material and locked.quantity > 0
        and need - locked.quantity <= TAIL_SQM
    ):
        # Последний лист после старого округления до сотых (XL-03): на полке
        # 6 листов, в системе 17.86 вместо 17.8608 — забираем всё, что есть.
        need = locked.quantity
    if locked.quantity < need:
        raise InsufficientStock(
            f"Недостаточно «{locked.name}»: нужно {need} кв.м, в наличии {locked.quantity}."
        )
    # КИМ раскроя (STK-07): у продажи куска со склада уходит площадь деталей ÷
    # КИМ — обрезки идут в себестоимость ЭТОЙ строки, а не в общие потери. Не
    # больше, чем лежит: деталь, которая влезла в последний лист, продаётся.
    kim = kim_factor(locked, need) if log_type == InventoryLog.Type.SALE else None
    kim_extra = Decimal("0")
    if kim:
        kim_extra = max(min((need / kim).quantize(Decimal("0.0001")), locked.quantity) - need, Decimal("0"))
        need += kim_extra
        if kim_extra:
            reason = (
                f"{reason} (КИМ {locked.kim_percent.normalize():f} %: "
                f"+{kim_extra.normalize():f} кв.м обрезков)"
            ).strip()

    was_above = locked.quantity > locked.critical_balance
    cogs = Decimal("0")
    remaining = need
    rolls = list(
        Roll.objects.select_for_update()
        .filter(material=locked, remaining_area__gt=0)
        .order_by("received_at", "pk")
    )
    if preferred_roll is not None:
        pk = getattr(preferred_roll, "pk", preferred_roll)
        chosen = next((r for r in rolls if r.pk == pk), None)
        if chosen is not None:
            rolls = [chosen] + [r for r in rolls if r.pk != chosen.pk]

    moves = []
    for roll in rolls:
        if remaining <= 0:
            break
        take = min(roll.remaining_area, remaining)
        roll.remaining_area -= take
        roll.save(update_fields=["remaining_area"])
        # Закуп × взято / принято — без копеечного хвоста цены кв.м (STK-10).
        cogs += roll.cost_of(take)
        remaining -= take
        moves.append((roll.pk, -take))
        if trace is not None:
            trace.append((roll.pk, take, None))

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
            # Почём ушло — по партиям, из которых взяли. У продажи то же число
            # лежит на строке чека; у списания и отхода строки чека нет, и
            # журнал — единственное место, где эта цифра остаётся.
            cost=cogs.quantize(Decimal("0.01")),
            created_by=user,
        )
        if happened_at:
            entry.happened_at = happened_at
        entry.save()
        record_lot_moves(entry, moves)

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
    trace=None,
) -> Decimal:
    """Списать `metres` погонных метров, идя по рулонам FIFO.

    ``trace`` — как у `consume_area`: `(рулон, площадь, метры)` по каждому
    рулону, с которого резали.

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
        .order_by("received_at", "pk")
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
    moves = []
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
        cogs += roll.cost_of(take_area)
        area_taken += take_area
        remaining -= take_m
        moves.append((roll.pk, -take_area))
        if trace is not None:
            trace.append((roll.pk, take_area, take_m))

    locked.quantity -= area_taken
    locked.save(update_fields=["quantity", "updated_at"])

    if log_type:
        entry = InventoryLog(
            type=log_type,
            material=locked,
            quantity_changed=-area_taken,
            # Метры считаны по ширине КАЖДОЙ партии, из которой резали, —
            # постфактум из площади их уже не восстановить.
            metres_changed=-(need - remaining),
            reason=reason,
            receipt=receipt,
            created_by=user,
        )
        if happened_at:
            entry.happened_at = happened_at
        entry.save()
        record_lot_moves(entry, moves)

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
    lots=None,
) -> None:
    """Вернуть `metres` погонных метров — зеркало `consume_metres`.

    Доливаем рулоны от старых к новым, каждый не выше его исходной площади, и
    переводим метры в площадь ЕГО шириной: рулон должен вернуться ровно в то
    состояние, из которого его резали.

    ``lots`` — `(рулон, площадь, метры)`, записанные при продаже: если строку
    резали с нескольких рулонов, каждому возвращается ровно его доля. Без них
    (старые строки) — прежнее поведение.
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    add = Decimal(metres)
    if add <= 0:
        return
    rolls = list(
        Roll.objects.select_for_update().filter(material=locked).order_by("received_at", "pk")
    )
    if preferred_roll is not None:
        # Выбранный рулон встаёт первым, остальные — обычным порядком.
        pk = getattr(preferred_roll, "pk", preferred_roll)
        chosen = next((r for r in rolls if r.pk == pk), None)
        if chosen is not None:
            rolls = [chosen] + [r for r in rolls if r.pk != chosen.pk]
    remaining = add
    area_added = Decimal("0")
    moves = []
    by_pk = {r.pk: r for r in rolls}
    for pk, part_area, part_metres in lots or ():
        roll = by_pk.get(pk)
        if remaining <= 0:
            break
        if roll is None or not roll.width:
            continue
        headroom_area = roll.initial_area - roll.remaining_area
        if headroom_area <= 0:
            continue
        want_m = part_metres if part_metres is not None else part_area / roll.width
        give_m = min(headroom_area / roll.width, want_m, remaining)
        give_area = give_m * roll.width
        roll.remaining_area += give_area
        roll.save(update_fields=["remaining_area"])
        area_added += give_area
        remaining -= give_m
        moves.append((roll.pk, give_area))
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
        moves.append((roll.pk, give_area))
    # Излишек (вернули больше, чем резали) кладём на самый свежий рулон, чтобы
    # остаток материала не разошёлся с суммой рулонов.
    if remaining > 0:
        target = next((r for r in reversed(rolls) if r.width), None)
        if target:
            extra_area = remaining * target.width
            target.remaining_area += extra_area
            target.save(update_fields=["remaining_area"])
            area_added += extra_area
            moves.append((target.pk, extra_area))

    locked.quantity += area_added
    locked.save(update_fields=["quantity", "updated_at"])
    if log_type:
        entry = InventoryLog(
            type=log_type,
            material=locked,
            quantity_changed=area_added,
            # Вернули столько же метров, сколько отрезали, — в журнале возврат
            # рулона тоже читается метрами.
            metres_changed=add,
            reason=reason,
            receipt=receipt,
            created_by=user,
        )
        if happened_at:
            entry.happened_at = happened_at
        entry.save()
        record_lot_moves(entry, moves)
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
    #
    # Недостача по промеру — деньги: себестоимость пропавших метров по цене
    # ЭТОГО рулона. Без неё недомер уходил в «списано без себестоимости» и в
    # прибыль не попадал никогда.
    if delta_area:
        entry = InventoryLog.objects.create(
            type=InventoryLog.Type.ADJUSTMENT,
            material=material,
            quantity_changed=delta_area,
            metres_changed=(counted - expected),
            reason=(
                f"Промер рулона {locked_roll.code or f'№{locked_roll.pk}'}: "
                f"было {expected} м, намерено {counted} м "
                f"({act.get_reason_code_display()})"
            ),
            # Излишек промера — тоже деньги (PNL-04): по цене этого рулона, со
            # знаком «+» (`quantity_changed` > 0). ОПиУ считает потери нетто.
            cost=locked_roll.cost_of(abs(delta_area)).quantize(Decimal("0.01")),
            created_by=user,
        )
        record_lot_moves(entry, [(locked_roll.pk, delta_area)])
    return act


@transaction.atomic
def write_off_roll(roll: Roll, metres: Decimal, *, reason: str = "", user=None,
                   happened_at=None) -> Decimal:
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
    entry = InventoryLog(
        type=InventoryLog.Type.WRITE_OFF,
        material=material,
        quantity_changed=-area,
        metres_changed=-metres,
        reason=f"{reason} Рулон {label}: {metres.normalize():f} м".strip(),
        # По цене ЭТОГО рулона: закуп × списанная площадь / принятая (STK-10) —
        # для целых метров то же, что метры × цена метра, но без хвоста
        # округления цены метра и с хвостом рулона, ушедшим целиком.
        cost=locked_roll.cost_of(area).quantize(Decimal("0.01")),
        created_by=user,
    )
    # Дата самой операции: отход, как и приход, вносят задним числом.
    if happened_at:
        entry.happened_at = happened_at
    entry.save()
    record_lot_moves(entry, [(locked_roll.pk, -area)])
    if was_above and material.quantity <= material.critical_balance:
        from integrations.telegram import notify_low_stock

        notify_low_stock(material)
    roll.refresh_from_db()
    return area


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
    preferred_roll=None,
    lots=None,
    newest_first: bool = False,
) -> Decimal:
    """Return `area` кв.м back to stock (refund).

    Возвращает СТОИМОСТЬ вернувшегося по партиям, куда он лёг (закуп ×
    площадь / принято; сверх партий — по последней закупочной, как его
    оценивает `Material.stock_value`). У инвентаризации (`log_type` =
    ADJUSTMENT) она пишется в журнал: излишек — это деньги, и ОПиУ гасит им
    недостачу (F5/PNL-04).

    ``newest_first`` — излишек инвентаризации: доливаем партии с САМОЙ СВЕЖЕЙ,
    у которой есть место. Недостача уходит FIFO со старейшей непустой, значит
    «ошибся — поправил» должен вернуть материал туда же; при доливе со
    старейшей он лёг бы в давно пустую партию по другой цене, и убыток
    остался бы разницей цен.

    ``lots`` — `(партия, площадь, …)`, записанные при продаже этой строки:
    каждая партия получает обратно ровно то, что с неё взяли. Продажа из двух
    партий (1000 + 3000) раньше после возврата целиком ложилась в старейшую —
    остаток дорогой партии пропадал, а следующие продажи шли по дешёвой цене.
    Без записей (строки, проданные до учёта партий) — прежнее поведение.

    Mirrors the FIFO drawdown: refills lots oldest-first, each only up to its
    original capacity (initial_area), so a refund spanning several lots restores
    them in the same order they were consumed and never inflates a roll past its
    initial area. Any surplus that no lot can hold (e.g. restoring more than was
    consumed) lands on the newest roll so material.quantity stays consistent with
    the sum of roll remainders.

    ``preferred_roll`` — партия, из которой продавали: она пополняется первой.
    Иначе возврат лёг бы в старейшую пачку, а не в ту, из которой лист брали,
    и остаток каждой партии перестал бы отвечать тому, что лежит на полке.
    """
    locked = Material.objects.select_for_update().get(pk=material.pk)
    add = Decimal(area)
    if add <= 0:
        return Decimal("0")
    rolls = list(
        Roll.objects.select_for_update().filter(material=locked).order_by("received_at", "pk")
    )
    newest = rolls[-1] if rolls else None
    if preferred_roll is not None:
        pk = getattr(preferred_roll, "pk", preferred_roll)
        chosen = next((r for r in rolls if r.pk == pk), None)
        if chosen is not None:
            rolls = [chosen] + [r for r in rolls if r.pk != chosen.pk]
    by_pk = {r.pk: r for r in rolls}
    # Возврат строки, проданной с КИМ (STK-07): её списание было площадь ÷ КИМ,
    # и в себестоимости строки сидят обрезки. Возврат сторнирует себестоимость
    # строки целиком — значит и на склад возвращается всё, что она забрала, а
    # не одна площадь деталей; иначе обрезки пропадали бы со склада без следа.
    kim = kim_factor(locked, add) if log_type == InventoryLog.Type.RETURN and lots else None
    if kim:
        taken = sum((a for pk, a, _m in lots if pk in by_pk), Decimal("0"))
        add = max(add, min((add / kim).quantize(Decimal("0.0001")), taken))
    remaining = add
    value = Decimal("0")
    moves = []
    for pk, part_area, _metres in lots or ():
        roll = by_pk.get(pk)
        if roll is None or remaining <= 0:
            continue
        headroom = roll.initial_area - roll.remaining_area
        give = min(headroom, part_area, remaining)
        if give <= 0:
            continue
        roll.remaining_area += give
        roll.save(update_fields=["remaining_area"])
        value += roll.cost_of(give)
        remaining -= give
        moves.append((roll.pk, give))
    for roll in (reversed(rolls) if newest_first else rolls):
        if remaining <= 0:
            break
        headroom = roll.initial_area - roll.remaining_area
        if headroom <= 0:
            continue
        give = min(headroom, remaining)
        roll.remaining_area += give
        roll.save(update_fields=["remaining_area"])
        value += roll.cost_of(give)
        remaining -= give
        moves.append((roll.pk, give))
    if remaining > 0 and newest is not None:
        newest.remaining_area += remaining
        newest.save(update_fields=["remaining_area"])
        value += newest.cost_of(remaining)
        moves.append((newest.pk, remaining))
    elif remaining > 0:
        value += remaining * (locked.purchase_price or Decimal("0"))
    locked.quantity += add
    locked.save(update_fields=["quantity", "updated_at"])
    if log_type:
        entry = InventoryLog.objects.create(
            type=log_type,
            material=locked,
            quantity_changed=add,
            reason=reason,
            receipt=receipt,
            created_by=user,
            cost=(value.quantize(Decimal("0.01"))
                  if log_type == InventoryLog.Type.ADJUSTMENT else None),
            **({"happened_at": happened_at} if happened_at else {}),
        )
        record_lot_moves(entry, moves)
    return value


def has_lots(material: Material) -> bool:
    """Есть ли у материала хоть одна НЕПУСТАЯ партия.

    Развилка для ШТУЧНОГО материала: с 2026-08-27 приход заводит ему партию, и
    продавать такой запас надо через FIFO — по цене той партии, из которой
    берём. Но материалы, заведённые раньше, партий не имеют: их остаток
    поднимали числом, и списывать его через FIFO не из чего. Поэтому решает не
    флаг в карточке, а факт — лежит ли товар по партиям.

    У площадного материала эта проверка не нужна: он партиями живёт всегда.
    """
    return material.rolls.filter(remaining_area__gt=0).exists()


def take_out(material: Material, qty: Decimal, *, log_type: str, reason: str = "",
             user=None, happened_at=None, preferred_roll=None) -> Decimal:
    """Убрать материал со склада МИМО продажи (брак, недостача, отход) и вернуть,
    во сколько он обошёлся.

    Один путь на всех, кто списывает: развилка «через партии или по карточке»
    жила в каждом вызывающем своя, и в списании брака её не было вовсе. Штучный
    товар с партиями уходил одним числом остатка: партия продолжала числить
    выброшенную штуку, а себестоимость в журнал не писалась — брак на 800 сом
    уходил из склада и не появлялся ни в «Списано», ни в прибыли, только в
    строке «Не объяснено».

    Площадной и штучный с партиями — FIFO по партиям (`consume_area`, выбранная
    партия первой), себестоимость по ним же. Штучный без партий — по закупочной
    из карточки: другой цены у такого запаса нет (так же его оценивает
    `Material.stock_value`).
    """
    from .stock import apply_stock_change

    qty = Decimal(qty)
    if qty <= 0:
        return Decimal("0")
    if material.is_roll_material or has_lots(material):
        return consume_area(
            material, qty, user=user, reason=reason, log_type=log_type,
            happened_at=happened_at, preferred_roll=preferred_roll,
        )
    cost = (qty * (material.purchase_price or Decimal("0"))).quantize(Decimal("0.01"))
    apply_stock_change(
        material, -qty, log_type=log_type, reason=reason, user=user,
        happened_at=happened_at, cost=cost,
    )
    return cost


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
    # Снятый хвост оценивался последней закупочной (`Material.stock_value`) —
    # по ней же и уходит: иначе склад дешевеет, а в «Списано» ноль. Поднятый
    # (партии знали больше числа) — тем, на сколько подорожал склад: это
    # излишек, ОПиУ гасит им потери (F5/PNL-04).
    before = Material.objects.prefetch_related("rolls").get(pk=locked.pk).stock_value
    locked.quantity = target
    locked.save(update_fields=["quantity", "updated_at"])
    after = Material.objects.prefetch_related("rolls").get(pk=locked.pk).stock_value
    tail_cost = (
        (-delta * (locked.purchase_price or Decimal("0"))).quantize(Decimal("0.01"))
        if delta < 0
        else (after - before)
    )
    InventoryLog.objects.create(
        type=InventoryLog.Type.ADJUSTMENT,
        material=locked,
        quantity_changed=delta,
        reason=(
            f"Сведение остатка с рулонами: было {target - delta} кв.м, "
            f"по рулонам {target} кв.м"
        ),
        cost=tail_cost,
        created_by=user,
    )
    material.refresh_from_db()
    return delta
