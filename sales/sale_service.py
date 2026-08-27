"""Core sales business logic: build a receipt, deduct stock, handle payment
confirmation and refunds. Kept separate from the views so it can be reused by
the payment webhook and tested in isolation.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from finance import cash
from warehouse.models import InventoryLog, Material
from warehouse.rolls import (
    consume_area,
    consume_metres,
    restore_area,
    restore_metres,
)
from warehouse.stock import apply_stock_change

from .models import Payment, Receipt, TransactionItem


def _money(value: Decimal) -> Decimal:
    """До копеек. Без этого SQLite сохранил бы «сырой» результат умножения, а
    PostgreSQL округлил бы его сам — и цифры на dev и на проде разошлись бы."""
    return Decimal(value).quantize(Decimal("0.01"))


# Площадь куска — до трёх знаков, «половина вверх». Столько хранит колонка
# `TransactionItem.quantity`, и по ней считается цена строки и списание.
# Раньше площадь шла в базу сырой (0.45 × 1.23 = 0.5535): SQLite так и хранил,
# PostgreSQL округлял сам до 0.554 — а касса на экране резала до 0.553. Три
# разных числа для одного куска давали три разных итога; теперь правило одно,
# и касса (`utils/area.js`) считает по нему же.
QTY_STEP = Decimal("0.001")


def _qty(value) -> Decimal:
    """Количество строки чека — до трёх знаков, «половина вверх» (как колонка)."""
    return Decimal(str(value)).quantize(QTY_STEP, rounding=ROUND_HALF_UP)


def _area(width, length) -> Decimal:
    return _qty(Decimal(str(width)) * Decimal(str(length)))


def _deduct(material, qty, user, reason="", receipt=None, happened_at=None,
            preferred_roll=None) -> Decimal:
    """Deduct stock, routing roll-materials through FIFO area consumption.

    Возвращает СЕБЕСТОИМОСТЬ списанного — её мы фиксируем на строке чека, чтобы
    прибыль считалась «выручка − себестоимость проданного», а не только за
    вычетом накладных расходов.

    Каждое списание пишется в складской журнал типом ПРОДАЖА со ссылкой на чек:
    иначе материал уходит со склада бесследно и на вопрос «куда делся» отвечать
    нечем. ``happened_at`` — дата заказа: у заказа задним числом расход должен
    стоять его датой, а не сегодняшней.
    """
    if qty <= 0:
        return Decimal("0")
    if material.is_roll_material:
        # FIFO знает, из каких именно партий ушёл материал и почём.
        return consume_area(
            material, qty, user=user, reason=reason,
            log_type=InventoryLog.Type.SALE, receipt=receipt, happened_at=happened_at,
            preferred_roll=preferred_roll,
        )
    apply_stock_change(
        material, -qty, user=user, reason=reason,
        log_type=InventoryLog.Type.SALE, receipt=receipt, happened_at=happened_at,
    )
    # У штучных материалов партий нет — берём текущую закупочную цену.
    return qty * (material.purchase_price or Decimal("0"))


def _restore(material, qty, user, reason="", receipt=None, happened_at=None,
             preferred_roll=None) -> Decimal:
    """Вернуть материал на склад при возврате заказа.

    Тип ВОЗВРАТ, а не «корректировка»: корректировка — это инвентаризация, а
    здесь у прихода есть парный расход по тому же чеку.
    """
    if qty <= 0:
        return Decimal("0")
    if material.is_roll_material:
        restore_area(
            material, qty, user=user, reason=reason,
            log_type=InventoryLog.Type.RETURN, receipt=receipt,
            preferred_roll=preferred_roll,
        )
    else:
        apply_stock_change(
            material, qty, log_type=InventoryLog.Type.RETURN,
            reason=reason, user=user, receipt=receipt,
        )
    return Decimal("0")


def _reason(receipt: Receipt, *, restore: bool, service=None) -> str:
    """Причина движения для журнала — фраза, понятная без соседних колонок.

    Журнал читают и в интерфейсе, и в админке, поэтому строка самодостаточная:
    «Продажа по чеку №12 — Вывеска для кафе».
    """
    number = f"№{receipt.order_number}" if receipt.order_number else ""
    tail = f" — {receipt.title}" if receipt.title else ""
    if service is not None:
        head = "Возврат материала услуги" if restore else "Расход материала на услугу"
        return f"{head} «{service.name}», чек {number}".strip() + tail
    head = "Возврат по чеку" if restore else "Продажа по чеку"
    return f"{head} {number}".strip() + tail


def _stock_was_deducted(receipt: Receipt) -> bool:
    """Уходил ли материал этого чека со склада.

    Наличный заказ списывает склад при оформлении, ОНЛАЙН — только когда шлюз
    подтвердил оплату (`confirm_payment`). Значит по неоплаченному онлайн-счёту
    возвращать на склад НЕЧЕГО: ничего оттуда и не брали.

    Условие держит и правка состава, и возврат — а удаление чека его не имело, и
    брошенный онлайн-заказ при удалении дорисовывал на склад свои позиции.

    `payment_status` в проверке — подстраховка на случай чека, у которого флаг
    не проставлен, а деньги приняты: ошибиться в сторону «списание было» здесь
    безопаснее, чем потерять материал, который действительно уходил.
    """
    return receipt.stock_deducted or receipt.payment_status in (
        Receipt.PaymentStatus.PAID,
        Receipt.PaymentStatus.PARTIALLY_REFUNDED,
    )


def _unarchive_returned(material: Material, receipt: Receipt, user) -> None:
    """Товар вернулся на полку — значит он снова существует.

    Материал с продажами не удаляется, а ПРЯЧЕТСЯ. И это верно, пока его нет в
    наличии. Но после возврата он снова лежит на складе: остаток и партии FIFO
    поднимаются как надо, а увидеть их негде — скрытого материала нет ни в
    каталоге, ни в кассе. Со стороны владельца это выглядело как «сделал
    возврат, а на склад ничего не вернулось».

    Поэтому возврат снимает пометку «скрыт» и объясняет это в журнале действий:
    решение принял не человек, и он должен понимать, откуда материал снова
    появился в каталоге.
    """
    if not material.is_archived:
        return
    from audit.models import AuditLog

    material.is_archived = False
    material.save(update_fields=["is_archived", "updated_at"])
    number = receipt.order_number or receipt.pk
    AuditLog.record(
        user,
        f"Материал «{material.name}» возвращён в каталог: по чеку {number} "
        "оформлен возврат, и товар снова на складе",
    )


def service_item_area(item: TransactionItem) -> Decimal:
    """Площадь, к которой относится строка услуги, кв.м.

    У резки `quantity` — ДЛИНА РЕЗА в погонных метрах, а площадь куска лежит в
    `width × length` (у реза целого листа размеров нет — площадь 0). У прочих
    площадных услуг (внутренний монтаж) количество и есть площадь.
    """
    if item.service_id and item.service.uses_running_meter:
        if item.width and item.length:
            return _area(item.width, item.length)
        return Decimal("0")
    return item.quantity


def recipe_consumption(recipe, item: TransactionItem) -> Decimal:
    """Сколько расходника техкарты уходит на строку услуги.

    «На кв.м» — от ПЛОЩАДИ куска, «фикс» — раз на строку. Раньше норма «на
    кв.м» умножалась на `item.quantity`, а у резки это погонные метры реза:
    0.1 клея на кв.м при куске 0.5 кв.м и 8 пог.м реза списывало 0.8 вместо
    0.05 — в 16 раз больше. Одна формула здесь и в обзоре
    (`materials_consumed_by_services`).
    """
    from services.models import ServiceRecipe

    if recipe.consumption_mode == ServiceRecipe.Mode.PER_SQM:
        return recipe.consumption_per_unit * service_item_area(item)
    return recipe.consumption_per_unit


def _deduct_stock_for_item(item: TransactionItem, user, *, restore=False) -> None:
    """Deduct (or restore) stock for a single line item.

    Cutting now produces two separate lines (a MATERIAL line for the cut material
    and a SERVICE line for the master's work), so the MATERIAL line handles its
    own area; service lines only consume their recipe (technological-card) extras.

    Каждое движение попадает в складской журнал со ссылкой на чек — и продажа
    материала, и расход по техкарте услуги (клей, крепёж).
    """
    fn = _restore if restore else _deduct
    receipt = item.receipt
    # Расход материала датируем заказом (в т.ч. задним числом), а возврат —
    # «сейчас»: возврат случается тогда, когда его оформили, а не когда продали.
    extra = {} if restore else {"happened_at": receipt.created_at}
    if item.type == TransactionItem.Type.MATERIAL and item.material_id:
        # РУЛОН идёт своим путём — погонными метрами по рулонам.
        #
        # Перевести метры в площадь одним умножением нельзя: у каждого рулона
        # своя ширина, замороженная при приёмке, и 1.4 м оракала шириной 1.0 —
        # это другая площадь и другая себестоимость, чем 1.4 м шириной 1.52.
        # `consume_metres` идёт по рулонам FIFO и у каждого переводит метры ЕГО
        # шириной; со склада уходит вся ширина полотна (режут поперёк целиком,
        # узкая полоса остаётся обрезком цеха).
        if item.sale_mode == TransactionItem.SaleMode.METER:
            # Рулон не выбран (дозаказ, повтор) — берём тот, с которого FIFO и
            # начнёт: строка чека должна помнить рулон, иначе обрезок и площадь
            # резки считались бы по ширине карточки, а возврат уехал бы не туда.
            if not restore and item.roll_id is None:
                from warehouse.models import Roll

                first = (
                    Roll.objects.filter(
                        material=item.material, remaining_area__gt=0, width__isnull=False
                    )
                    .order_by("received_at")
                    .first()
                )
                if first is not None:
                    item.roll = first
                    item.save(update_fields=["roll"])
            metre_fn = restore_metres if restore else consume_metres
            cost = metre_fn(
                item.material, item.quantity, user=user,
                reason=_reason(receipt, restore=restore),
                log_type=(
                    InventoryLog.Type.RETURN if restore else InventoryLog.Type.SALE
                ),
                receipt=receipt,
                # Резали из этого рулона — в него же и возвращаем. Иначе метры
                # «переезжали» бы в соседний, и остаток каждого физического
                # рулона переставал бы совпадать с тем, что лежит на полке.
                preferred_roll=item.roll_id,
                **extra,
            )
            if not restore:
                item.cost_total = _money(cost or Decimal("0"))
                item.save(update_fields=["cost_total"])
            else:
                _unarchive_returned(item.material, receipt, user)
            return
        # Whole-piece sales deduct the piece area; area/qty sales deduct quantity.
        qty = item.quantity
        if item.sale_mode == TransactionItem.SaleMode.PIECE and item.material.piece_area:
            qty = item.material.piece_area * item.quantity
        # У ЛИСТА партии тоже есть, и мастер может взять лист из конкретной
        # пачки: партия строки чека уходит в списание первой, остальные — за
        # ней обычным FIFO. Партию запоминаем в строке (как у рулона), иначе
        # возврат вернул бы листы не в ту пачку, а себестоимость строки
        # перестала бы сходиться с той, по которой продали.
        if item.material.is_roll_material:
            if not restore and item.roll_id is None:
                from warehouse.models import Roll

                first = (
                    Roll.objects.filter(material=item.material, remaining_area__gt=0)
                    .order_by("received_at")
                    .first()
                )
                if first is not None:
                    item.roll = first
                    item.save(update_fields=["roll"])
            extra = {**extra, "preferred_roll": item.roll_id}
        cost = fn(
            item.material, qty, user,
            reason=_reason(receipt, restore=restore), receipt=receipt, **extra,
        )
        if not restore:
            item.cost_total = _money(cost)
            item.save(update_fields=["cost_total"])
        else:
            _unarchive_returned(item.material, receipt, user)
        return
    if item.type != TransactionItem.Type.SERVICE or not item.service_id:
        return

    # Extra recipe materials (e.g. fasteners for installation, glue, …) — их
    # себестоимость тоже относим на строку услуги.
    cost = Decimal("0")
    reason = _reason(receipt, restore=restore, service=item.service)
    for recipe in item.service.recipes.select_related("material").all():
        consumed = recipe_consumption(recipe, item)
        cost += fn(recipe.material, consumed, user, reason=reason, receipt=receipt, **extra)
        if restore:
            # Расходники техкарты возвращаются той же логикой, что и материал
            # строки: спрятанный клей после возврата тоже снова на складе.
            _unarchive_returned(recipe.material, receipt, user)
    if not restore and cost:
        item.cost_total = _money(cost)
        item.save(update_fields=["cost_total"])


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
        qty = _qty(entry.get("quantity") or 0)
        if mode == TransactionItem.SaleMode.PIECE:
            # Опт: при заказе от wholesale_min_qty листов цена за лист сама
            # переключается на оптовую (если её задал админ). Ручной override
            # цены (если есть) всегда в приоритете.
            piece = material.piece_price_for_qty(qty)
            # У ШТУЧНОГО материала (крепёж, клей) цены «за лист» не существует —
            # там она всегда 0, и продажа уходила за 0 сом. Цена за штуку у него
            # обычная розничная. У листового материала 0 в piece_price означает
            # другое — «продажа листом недоступна», и подменять его нельзя.
            if not piece and not material.is_roll_material:
                piece = material.price_per_unit
            price = _priced("material_price", piece)
        elif mode == TransactionItem.SaleMode.METER:
            # Рулон продаётся ДЛИНОЙ: количество строки — метры полотна, цена —
            # за погонный метр. Площадь тут ни при чём: ширину клиент не
            # выбирает, поперёк режут на всю. Через площадь цифра сходилась бы
            # только если прайс поделить на ширину (300 ÷ 0.9 = 333.33) и ширину
            # намертво зашить — а владелец держит прайс в метрах и делить в уме
            # не станет.
            #
            # Режим приходит ЯВНО и не подставляется по справочнику. Соблазн
            # «сервер сам поймёт, что это рулон» опасен: касса в режиме площади
            # шлёт в `quantity` ПЛОЩАДЬ, и молчаливая подмена превратила бы
            # 1.26 кв.м в 1.26 пог.м — цифра выглядит правдоподобно, а заказ
            # посчитан не по тому. Форму выбирает касса по `sells_by_metre`,
            # сервер лишь проверяет, что прислали.
            price = _priced("material_price", material.price_per_pm)
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
            # Партию запоминаем на строке: из неё списывали, в неё же вернём
            # при возврате, и по ней в чеке пишется «списано с рулона №7».
            # Не только у рулона: у листа пачки тоже разные по цене закупки, и
            # мастер может взять лист из той, что стоит ближе.
            roll=entry.get("roll") if material.is_roll_material else None,
            # Ширина изделия — чтобы посчитать обрезок. Полную ширину списали и
            # деньги за неё взяли; сколько из этого ушло в мусор, без неё не
            # узнать никак.
            used_width=(
                entry.get("used_width") if mode == TransactionItem.SaleMode.METER else None
            ),
        )]

    service = entry["service"]

    # Area-priced services: cutting and interior install. Work is computed
    # automatically from the cut area (width × length); no manual entry.
    if service.uses_area:
        width = entry.get("width")
        length = entry.get("length")
        area = _area(width, length) if width and length else _qty(entry.get("quantity") or 0)
        material = entry.get("material")

        # Резка → ставка СТАНКА, если она задана, иначе ставка материала.
        # Станок впереди специально: иначе выбор «ЧПУ / лазер» не менял бы цену,
        # и «выбрал лазер, а сумма та же» читалось бы как поломка. Ноль у станка
        # означает «своей ставки нет» — тогда всё как до разделения, по
        # материалу. Внутренний монтаж → ставка услуги за кв.м.
        # Любую ставку админ всё так же может перебить в момент продажи.
        if service.uses_running_meter:
            fallback = material.cut_rate_per_pm if material else Decimal("0")
            rate = _priced("cut_rate", service.rate_per_pm or fallback)
        else:
            rate = _priced("cut_rate", service.rate_flat)
        # Резку считаем по ДЛИНЕ РЕЗА в погонных метрах — её вводит мастер.
        # Площадь вместо длины сюда НЕ подставляется: так уже было, кв.м
        # считались как пог.м (для листа 1.22×2.44 — 2.98 вместо реальных 7.32).
        # Пустая длина в заказ не проходит — её отклоняет
        # `SaleItemInputSerializer` (обе ручки: касса и дозаказ), иначе фигурный
        # рез уезжал в чек нулём. Ноль здесь остаётся только для прямых вызовов
        # изнутри (тесты, seed): API до этой строки с пустой длиной не доходит.
        # Для не-резочных площадных услуг (внутренний монтаж) — по-прежнему площадь.
        work_qty = area
        if service.uses_running_meter:
            rm = entry.get("running_meters")
            work_qty = _qty(rm) if rm not in (None, "") else Decimal("0")
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
                # Режут из ВЫБРАННОЙ пачки — как и при обычной продаже листа.
                roll=entry.get("roll") if material.is_roll_material else None,
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


def client_change_available(client, *, exclude=None) -> Decimal:
    """Сдача клиента, которую ему ещё не отдали, по всем его заказам."""
    if client is None:
        return Decimal("0")
    qs = Receipt.objects.filter(client=client, change_due__gt=0)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return sum((r.change_due for r in qs), Decimal("0"))


@transaction.atomic
def _take_client_change(client, amount, *, exclude=None) -> Decimal:
    """Погасить `amount` из сдачи клиента, начиная с самых старых заказов.

    Сдача лежит НА ЗАКАЗАХ, а не общим балансом клиента: 1 500 по позавчерашней
    вывеске и 200 по вчерашним визиткам — это две разные строки, и выдают их
    тоже по заказам. Забираем с самого старого: он ждёт дольше всех.

    `atomic` здесь не ради отката, а ради `select_for_update` ниже: на Postgres
    он падает вне транзакции. Сейчас единственный вызов идёт из `create_sale`,
    которая атомарна, — но полагаться на это значит держать мину под деньгами
    клиента до первого нового вызова. Вложенный `atomic` — просто точка
    сохранения, стоит он ничего.
    """
    left = Decimal(amount)
    taken = Decimal("0")
    qs = Receipt.objects.filter(client=client, change_due__gt=0).order_by("created_at", "id")
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    for source in qs.select_for_update():
        if left <= 0:
            break
        part = min(source.change_due, left)
        source.change_due -= part
        source.save(update_fields=["change_due", "updated_at"])
        left -= part
        taken += part
    return taken


def _settle_old_debts(receipt, client, cashier, payment_method, debt_ids, *, surplus, pay_full):
    """Погасить долги прошлых заказов деньгами, принесёнными с новой продажей.

    Возвращает, сколько из ``surplus`` (принесённого сверх нового заказа) НЕ
    ушло в долги — это и будет сдача на новом чеке. С ``pay_full`` долги
    закрываются целиком, независимо от ``surplus``. Каждое погашение — обычная
    оплата долга (`apply_payment`): запись `Payment` и приход в кассу тем же
    способом оплаты. Не смогли (гонка: долги уже закрыты) — заказ всё равно
    оформлен, объяснение уходит в ``receipt.debt_error``.
    """
    receipt.debt_paid = Decimal("0")
    receipt.debt_error = ""
    if not debt_ids or client is None:
        return surplus
    if pay_full:
        want = None
    else:
        if surplus <= 0:
            return surplus
        wanted = {str(x) for x in debt_ids}
        owed = sum(
            (r.debt for r in client.receipts.exclude(pk=receipt.pk) if str(r.id) in wanted),
            Decimal("0"),
        )
        want = min(surplus, owed)
        if want <= 0:
            return surplus
    try:
        allocations, _left = pay_client_debt(
            client, want, receipt_ids=debt_ids, user=cashier, method=payment_method,
            note=f"С заказом №{receipt.order_number}",
        )
    except PaymentRejected as e:
        receipt.debt_error = str(e)
        return surplus
    receipt.debt_paid = sum((paid for _, paid in allocations), Decimal("0"))
    if want is None:
        # «Вся сумма»: заказ оплачен ровно, долги закрыты целиком — сдачи нет.
        return surplus
    # Из принесённого сверх заказа ушло ровно `want`: что не легло по долгам
    # (`left`, гонка), `pay_client_debt` уже записал сдачей на последний
    # погашенный заказ — второй раз в новый чек это не возвращаем.
    return surplus - want


@transaction.atomic
def create_sale(
    *, client, cashier, payment_method, items_data, amount_paid=None, title="",
    created_at=None, pay_full=False, use_change=False, pay_debt_ids=None,
) -> Receipt:
    """Create a receipt with its line items.

    ``use_change=True`` — закрыть остаток заказа СДАЧЕЙ с прошлых заказов
    клиента. Раньше сдача просто висела: клиент принёс 10 000 за заказ на 9 000,
    мелочи в кассе не нашлось, и на следующем заказе эта тысяча в оплату не шла
    никак — её приходилось сначала выдавать на руки, а потом принимать обратно.
    Деньги при зачёте не двигаются (они с того раза лежат в кассе), поэтому в
    кассовую книгу он не пишется — но остаётся виден в чеке отдельной строкой.

    ``pay_full=True`` — «заплатил ровно сколько вышло»: сумма чека известна
    только здесь, после сборки строк, и вызывающий её заранее назвать не может.
    Раньше для этого передавали заведомо большое число (9 999 999), и оно молча
    обрезалось до суммы чека. С тех пор как переплата стала запоминаться сдачей,
    такой «сентинел» превращается в девять миллионов сдачи клиенту. Намерение
    должно быть названо, а не закодировано абсурдным числом.

    ``pay_debt_ids`` — прошлые заказы клиента, долг по которым он гасит ЭТИМИ
    ЖЕ деньгами. ``amount_paid`` тогда — всё, что клиент принёс: сначала
    закрывается новый заказ, остаток идёт в долги от старых к новым, и только
    то, что не пригодилось, остаётся сдачей на новом чеке; ``pay_full`` — «отдал
    всё»: заказ и долги целиком. Раньше сумма сверх заказа становилась сдачей,
    а долги закрывались ОТДЕЛЬНО и целиком, и кассир, вписавший «заказ + долг»
    (как и просила подсказка), получал двойной счёт: долг закрыт, у клиента
    «сдача» на ту же сумму, в кассе она записана дважды, а следующий заказ
    закрывался этой сдачей бесплатно. Результат — атрибуты ``debt_paid`` и
    ``debt_error`` на чеке (не поля модели).

    Cash sales are settled immediately (PAID + stock deducted). Online sales are
    created PENDING; stock is deducted only once payment is confirmed
    (see ``confirm_payment``).

    ``created_at`` — дата заказа; не задана, значит «сейчас». Задним числом её
    ставит только админ (проверка во вьюхе): по этой дате считается вся
    отчётность. Той же датой пишется и списание материала — иначе журнал склада
    показывал бы расход сегодня по заказу за прошлый месяц.
    """
    receipt = Receipt.objects.create(
        client=client,
        cashier=cashier,
        payment_method=payment_method,
        payment_status=Receipt.PaymentStatus.PENDING,
        title=(title or "").strip(),
        **({"created_at": created_at} if created_at else {}),
    )

    for entry in items_data:
        _build_item(receipt, entry)  # creates one or more line items

    total = receipt.recalculate_total()

    if payment_method != Receipt.PaymentMethod.ONLINE:
        # Наличные / MBank / DemirBank — товар отдаём сразу, поэтому склад
        # списывается независимо от оплаты. Сколько денег реально взяли —-
        # решает кассир: сумма НЕ указана значит не платили, и весь заказ
        # уходит в долг (раньше пустое поле молча означало «оплачено полностью»).
        #
        # ПЕРЕПЛАТА теперь запоминается сдачей, а не отбрасывается. Заказ на
        # 1500, принесли 3000, сдачи в кассе не было — 1500 остались у цеха, и
        # это его долг перед клиентом. Раньше здесь стоял `min(..., total)`, и
        # назавтра вспомнить, сколько за кем осталось, было нечем.
        # «Вся сумма» при включённом зачёте — это ОСТАТОК после сдачи: кассир
        # берёт с клиента 2 000 по заказу на 3 000, когда тысяча уже лежит у
        # цеха с прошлого раза. Считает это сервер, а не касса: сумму чека знает
        # только он, и расхождение округлений в сом оставляло фантомный долг.
        offset = (
            min(client_change_available(client, exclude=receipt), total)
            if use_change and client is not None
            else Decimal("0")
        )
        if pay_full:
            brought = total - offset
        elif amount_paid is None:
            brought = Decimal("0")
        else:
            brought = max(Decimal(str(amount_paid)), Decimal("0"))
        # Сдача с прошлых заказов идёт только на то, что не покрыли деньгами
        # (клиент, принёсший всю сумму наличными, свою сдачу не тратит). Но
        # когда этими же деньгами гасят и долг, сдача зачитывается в заказ
        # ПЕРВОЙ: касса называет «к получению» = заказ − сдача + долг, кассир
        # берёт ровно столько, и остаток сверх заказа должен уйти в долг, а не
        # осесть новой сдачей рядом с незакрытой сотней долга.
        if pay_debt_ids and use_change and offset > 0 and not pay_full:
            paid = min(brought, total - offset)
        else:
            paid = min(brought, total)
        _deduct_all(receipt)
        receipt.stock_deducted = True
        receipt.amount_paid = paid
        surplus = brought - paid
        # Долг прошлых заказов — из тех же принесённых денег: сначала этот
        # заказ, остаток — в долги от старых к новым, что не пригодилось —
        # сдача. «Вся сумма» с галочкой — заказ и долги целиком.
        surplus = _settle_old_debts(
            receipt, client, cashier, payment_method, pay_debt_ids,
            surplus=surplus, pay_full=pay_full,
        )
        receipt.change_due = surplus
        receipt.payment_status = (
            Receipt.PaymentStatus.PAID if paid >= total else Receipt.PaymentStatus.PENDING
        )
    else:
        _create_online_invoice(receipt)

    # Зачёт сдачи — ПОСЛЕ обычной оплаты и только на остаток: клиент, который
    # принёс всю сумму наличными, свою сдачу не тратит. Онлайн-счёт не трогаем:
    # там оплату подтверждает шлюз.
    if use_change and client is not None and payment_method != Receipt.PaymentMethod.ONLINE:
        owed = total - receipt.amount_paid
        if owed > 0:
            applied = _take_client_change(client, owed, exclude=receipt)
            if applied > 0:
                receipt.amount_paid += applied
                receipt.change_applied = applied
                receipt.payment_status = (
                    Receipt.PaymentStatus.PAID
                    if receipt.amount_paid >= total
                    else Receipt.PaymentStatus.PENDING
                )

    receipt.save()
    # Деньги, принятые при оформлении, — приход в кассовую книгу. Датируем
    # ДАТОЙ ЗАКАЗА: заказ задним числом принёс деньги тогда же, а не сегодня.
    #
    # В кассу кладём то, что клиент ПРИНЁС, а не то, что зачлось за заказ:
    # заказ на 36, принесли 100 — в ящике лежит 100, и 64 из них станут сдачей.
    # Списывается она при выдаче (`give_change`). Записывай мы сюда зачтённые 36,
    # выдача сдачи увела бы кассу в минус на ровном месте.
    #
    # Зачтённая сдача сюда тоже НЕ идёт: эти деньги лежат в кассе с прошлого
    # заказа, второй раз их не приносили.
    brought = receipt.amount_paid - receipt.change_applied + receipt.change_due
    if brought > 0:
        cash.receipt_paid(
            receipt, brought, user=cashier,
            happened_on=timezone.localtime(receipt.created_at).date(),
        )
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

    New items are priced/built like a normal sale. Returns (receipt, surcharge).

    СКЛАД. Новые строки уходят со склада тогда же, когда ушли остальные строки
    чека: наличный заказ списывается при оформлении — независимо от того,
    оплачен он или в долг, — и дозаказ в него списывается сразу; онлайн-счёт,
    который шлюз ещё не подтвердил, склад не трогал — и его дозаказ дождётся
    `confirm_payment`. Развилка — `_stock_was_deducted`, та же, что у возврата
    и удаления. Раньше здесь смотрели на СТАТУС ОПЛАТЫ (PAID / частичный
    возврат), и дозаказ в наличный заказ, оформленный в долг (PENDING), не
    списывался вовсе: лист уходил с полки, остаток не менялся, себестоимость
    строки была 0, а возврат такого чека клал на склад ДВА листа вместо одного.

    ДЕНЬГИ. Доплата — это долг, пока её не приняли. Чек, бывший «Оплачено»,
    после дозаказа снова ждёт оплаты на разницу: `Receipt.debt` считает её по
    числам, «Принять оплату» её видит. Раньше статус не трогали: заказ 3 700 →
    7 400 оставался PAID, долг был 0, «Принять оплату» отвечала «долга нет», в
    кассу ничего не попадало — доплата исчезала из всех отчётов разом (выручка
    7 400, «на руках» 3 700, долг 0). Частично возвращённый чек статус не
    меняет: он и так в `OWING_STATUSES`, долг по нему считается.

    Онлайн-счёт после дозаказа шлюзом не перевыставляется: доплату принимают
    через `/pay/` (наличными или переводом), как обычный долг.
    """
    if receipt.status == Receipt.Status.CANCELLED or receipt.payment_status == Receipt.PaymentStatus.REFUNDED:
        raise OrderClosed("Чек закрыт или возвращён — добавление невозможно.")
    # ВЫДАННЫЙ заказ дозаказу не подлежит: товар уже у клиента, он ушёл. Раньше
    # проверялся только статус оплаты, и в отданный заказ спокойно дописывались
    # позиции — сумма росла со 110 до 165, склад списывался, а у клиента на
    # руках оставался чек на старую сумму. Нужен ещё товар — это новый заказ.
    if receipt.fulfillment_status == Receipt.FulfillmentStatus.ISSUED:
        raise OrderClosed("Заказ уже выдан клиенту — оформите новый.")

    # По чеку с возвратом часть принесённых денег уже отдали клиенту; на руках
    # у цеха — не больше стоимости оставшихся строк. Фиксируем ДО дозаказа,
    # иначе доплата за новые строки спряталась бы за давно выданной суммой.
    if receipt.refunded_amount > 0:
        receipt.amount_paid = _money_held(receipt)

    deduct_now = _stock_was_deducted(receipt)
    surcharge = Decimal("0")
    for entry in items_data:
        for item in _build_item(receipt, entry):
            surcharge += item.line_total
            if deduct_now:
                _deduct_stock_for_item(item, user)

    receipt.recalculate_total()
    if (
        receipt.payment_status == Receipt.PaymentStatus.PAID
        and receipt.amount_paid < receipt.total_price - receipt.refunded_amount
    ):
        receipt.payment_status = Receipt.PaymentStatus.PENDING
    receipt.save(update_fields=["total_price", "amount_paid", "payment_status", "updated_at"])
    return receipt, surcharge


@transaction.atomic
def confirm_payment(receipt: Receipt) -> Receipt:
    """Called when the payment gateway confirms an online payment."""
    if receipt.payment_status == Receipt.PaymentStatus.PAID:
        return receipt
    _settle(receipt)
    receipt.save(update_fields=["payment_status", "amount_paid", "stock_deducted", "updated_at"])
    # Онлайн-оплата — такой же приход денег, как наличные в ящик и перевод на
    # карту, и в кассовую книгу она обязана попасть. Этой строки тут не было:
    # приход писали только `create_sale` и `apply_payment`, а онлайн-заказ шёл
    # мимо обоих. Чек становился «Оплачено», выручка в отчёте росла, а «Касса и
    # банк» этих денег не видела вовсе — свести остаток по счёту было нечем.
    #
    # Счёт выбирает `cash.account_for` по способу оплаты: ONLINE это не
    # наличные, значит банк. Дата — сегодняшняя (день подтверждения оплаты, а не
    # оформления заказа): деньги приходят именно тогда, когда их подтвердил шлюз.
    #
    # Идемпотентность держит проверка в начале функции: повторное подтверждение
    # того же чека выходит раньше и второй записи не делает.
    cash.receipt_paid(receipt, receipt.amount_paid, user=receipt.cashier)
    return receipt


class PaymentRejected(Exception):
    """Оплату принять нельзя. Текст исключения уходит пользователю как есть."""


def parse_amount(raw):
    """'1500' → Decimal. Пусто → None, что означает «весь остаток долга»."""
    if raw in (None, ""):
        return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise PaymentRejected("Некорректная сумма.")
    # NaN и ±Infinity разбираются в Decimal без ошибки, но NaN роняет сравнение
    # `<= 0` пятисоткой, а Infinity молча закрывает долг любого размера.
    if not amount.is_finite():
        raise PaymentRejected("Некорректная сумма.")
    if amount <= 0:
        raise PaymentRejected("Сумма должна быть больше 0.")
    return amount


def parse_paid_on(raw):
    """'YYYY-MM-DD' → date. Пусто → None, то есть «сегодня».

    Кривую дату отклоняем, а не подменяем сегодняшней: оплату проводят задним
    числом ради самой даты, и молча потерять её хуже, чем показать ошибку.
    Будущим числом оплату не принимаем — этих денег ещё нет.
    """
    if raw in (None, ""):
        return None
    try:
        parsed = date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise PaymentRejected("Некорректная дата оплаты.")
    if parsed > timezone.localdate():
        raise PaymentRejected("Дата оплаты не может быть в будущем.")
    return parsed


def day_to_moment(day):
    """Дата заказа (`date`) → момент времени для `Receipt.created_at`.

    Берём ПОЛДЕНЬ по местному времени, а не полночь: отчёты фильтруют по
    `created_at__date`, и полночь в Бишкеке — это вчерашний вечер по UTC, то
    есть заказ мог бы попасть в соседний день (и в соседний месяц на стыке).
    Полдень от такой ошибки далёк при любом смещении.
    """
    if day is None:
        return None
    naive = datetime.combine(day, time(12, 0))
    return timezone.make_aware(naive, timezone.get_current_timezone())


def receipt_owed(receipt: Receipt) -> Decimal:
    """Остаток к оплате по чеку: сумма − возвраты − уже принятое."""
    owed = receipt.total_price - receipt.refunded_amount - receipt.amount_paid
    return owed if owed > Decimal("0") else Decimal("0")


@transaction.atomic
def apply_payment(
    receipt: Receipt, amount=None, *, user=None, paid_on=None, method=None, note="", keep_change=False
) -> Decimal:
    """Принять оплату долга по чеку. Возвращает РЕАЛЬНО зачтённую сумму.

    `amount=None` — закрыть весь остаток. Больше остатка в долг не зачитываем:
    долг не может уйти в минус.

    `keep_change=True` — лишнее не выбрасывать, а записать СДАЧЕЙ (`change_due`):
    деньги принесли, а вернуть их на руки не смогли. Вызывающий говорит об этом
    явно, потому что общая выплата сама решает, куда девать остаток.

    Каждая оплата пишется записью ``Payment`` — с датой, которую можно поставить
    задним числом, и способом оплаты.
    """
    if receipt.status == Receipt.Status.CANCELLED:
        raise PaymentRejected("Чек отменён.")
    if receipt.payment_status not in (
        Receipt.PaymentStatus.PENDING,
        Receipt.PaymentStatus.PARTIALLY_REFUNDED,
    ):
        raise PaymentRejected("По этому чеку долга нет.")
    owed = receipt_owed(receipt)
    if owed <= 0:
        raise PaymentRejected("По этому чеку долга нет.")

    brought = owed if amount is None else amount
    amount = min(brought, owed)
    over = brought - amount if keep_change else Decimal("0")
    receipt.amount_paid = receipt.amount_paid + amount
    receipt.change_due = receipt.change_due + over
    if receipt.amount_paid >= receipt.total_price - receipt.refunded_amount:
        receipt.payment_status = Receipt.PaymentStatus.PAID
    receipt.save(
        update_fields=["amount_paid", "change_due", "payment_status", "updated_at"]
    )

    settled_on = paid_on or timezone.localdate()
    Payment.objects.create(
        receipt=receipt,
        amount=amount,
        method=method or receipt.payment_method,
        paid_on=settled_on,
        note=note or "",
        created_by=user,
    )
    # Погашение долга — такой же приход денег, как оплата в кассе, и датируется
    # днём, когда деньги реально принесли.
    cash.receipt_paid(
        receipt, amount, user=user, happened_on=settled_on,
        method=method or receipt.payment_method,
    )
    return amount


@transaction.atomic
def pay_client_debt(
    client, amount=None, *, receipt_ids=None, user=None, paid_on=None, method=None, note=""
):
    """Общая выплата: одной суммой гасим долги сразу нескольких заказов клиента.

    Клиент приходит раз в неделю и отдаёт деньги «за всё», а не по чеку — раньше
    это приходилось разносить руками, открывая каждый заказ отдельно.

    Гасим от старых заказов к новым: сначала то, что висит дольше. `amount=None`
    закрывает долги выбранных заказов целиком; `receipt_ids=None` берёт все
    заказы клиента с долгом. Возвращает `(распределение, остаток)` — куда именно
    ушли деньги и сколько не пригодилось.

    Остаток записывается СДАЧЕЙ на последний из погашенных заказов: деньги в
    кассе, отдать их могли не сразу, и в чьей они тумбочке — вопрос, на который
    система обязана отвечать. Раньше остаток просто возвращался числом и нигде
    не сохранялся.
    """
    wanted = {str(x) for x in receipt_ids} if receipt_ids is not None else None
    debts = [
        r
        for r in client.receipts.order_by("created_at")
        if r.debt > 0 and (wanted is None or str(r.id) in wanted)
    ]
    if not debts:
        raise PaymentRejected("У клиента нет заказов с долгом.")

    left = amount  # None — «сколько нужно, столько и закрываем»
    allocations = []
    for receipt in debts:
        if left is not None and left <= 0:
            break
        take = None if left is None else min(left, receipt.debt)
        paid = apply_payment(
            receipt, take, user=user, paid_on=paid_on, method=method, note=note
        )
        if left is not None:
            left -= paid
        allocations.append((receipt, paid))

    change = left if left is not None else Decimal("0")
    if change > 0 and allocations:
        # Сдачу вешаем на ПОСЛЕДНИЙ погашенный заказ — тот, на котором деньги
        # кончились. Он же ближе всех по времени, и искать сдачу клиент с
        # кассиром будут именно там.
        last = allocations[-1][0]
        last.change_due = last.change_due + change
        last.save(update_fields=["change_due", "updated_at"])
    return allocations, change


class ItemEditRejected(Exception):
    """Строку чека править нельзя (возвращена, чужой чек, кривые данные)."""


def _money_held(receipt: Receipt) -> Decimal:
    """Сколько денег по чеку РЕАЛЬНО лежит у цеха.

    `amount_paid` — сколько клиент принёс. После возврата часть этих денег ушла
    обратно (`refund_receipt` сразу отдаёт переплату относительно оставшихся у
    клиента строк), а поле остаётся прежним: из него же акт сверки и повторный
    возврат считают, что уже выдано. Поэтому на руках у цеха не больше, чем
    стоят оставшиеся строки. Считать это надо ДО того, как состав чека
    изменится: после дозаказа или правки «оставшееся» уже другое.
    """
    kept = receipt.total_price - receipt.refunded_amount
    if kept <= 0:
        return Decimal("0")
    return min(receipt.amount_paid, kept)


def _resettle(receipt: Receipt, *, held=None) -> None:
    """Пересчитать итог, статус оплаты и сдачу после правки состава.

    Если итог УПАЛ ниже уже принятых денег — разница не пропадает и не остаётся
    «переплатой»: она становится СДАЧЕЙ, которую цех должен клиенту. Ровно тот
    случай, ради которого правку и просили: написали лишний квадратный метр,
    клиент заплатил по завышенному счёту, потом это нашли.

    ``held`` — деньги на руках у цеха ДО правки (`_money_held`), если по чеку
    уже был возврат. Без этого чек с частичным возвратом считался бы по
    принесённой сумме целиком, хотя часть её клиенту уже отдали: уменьшение
    строки превращало давно выданные деньги в сдачу второй раз, а увеличение —
    прятало долг за той же выданной суммой.

    Статус «частичный возврат» правка не стирает: он в `OWING_STATUSES`, долг
    по нему виден, а откат оплаты по нему закрыт — и должен остаться закрытым.
    """
    if held is not None:
        receipt.amount_paid = held
    total = receipt.recalculate_total()
    owed_base = total - receipt.refunded_amount
    over = receipt.amount_paid - owed_base
    if over > 0:
        receipt.amount_paid = owed_base if owed_base > 0 else Decimal("0")
        receipt.change_due = receipt.change_due + over
    if receipt.payment_status != Receipt.PaymentStatus.PARTIALLY_REFUNDED:
        receipt.payment_status = (
            Receipt.PaymentStatus.PAID
            if receipt.amount_paid >= owed_base
            else Receipt.PaymentStatus.PENDING
        )
    receipt.save(
        update_fields=[
            "total_price",
            "amount_paid",
            "change_due",
            "payment_status",
            "updated_at",
        ]
    )


@transaction.atomic
def update_receipt_items(receipt: Receipt, changes, *, user=None) -> Receipt:
    """Править состав чека: количество, цену строки, удаление лишней строки.

    Ошибиться можно не только в наименовании: лишний лист, лишний квадратный
    метр, цена не та. Раньше на это был только один ответ — удалить чек целиком
    и завести заново, и это разумно ровно до момента, когда по заказу уже прошли
    оплаты, а из десяти строк неверна одна.

    Строку правим ЧЕРЕЗ СКЛАД, а не арифметикой по полям: возвращаем на склад
    ровно то, что этой строкой было списано, применяем правку и списываем
    заново. Так себестоимость пересчитывается сама (для рулонных — по партиям
    FIFO), а остаток не расходится с журналом.

    В журнале склада остаётся ОДНА запись продажи с исправленным количеством:
    техническая пара «возврат + новая продажа» из него убирается. Возврат — это
    когда клиент принёс заказ обратно; исправление опечатки возвратом называть
    нельзя, иначе лента движений врёт о том, что происходило в цехе.

    `changes` — список `{"id", "quantity"?, "price_per_item"?, "remove"?}`.
    """
    if receipt.status == Receipt.Status.CANCELLED:
        raise ItemEditRejected("Чек отменён — править его состав нельзя.")

    # Деньги на руках — до правки: по чеку с возвратом часть принесённого уже
    # отдали, и считать переплату/долг от полной суммы нельзя (см. `_resettle`).
    held = _money_held(receipt) if receipt.refunded_amount > 0 else None

    sale_before = list(
        receipt.inventory_logs.filter(type=InventoryLog.Type.SALE)
        .order_by("id")
        .values_list("id", "material_id")
    )
    log_ids_before = set(receipt.inventory_logs.values_list("id", flat=True))

    for change in changes:
        try:
            item = receipt.items.get(pk=change["id"])
        except (TransactionItem.DoesNotExist, KeyError, ValueError):
            raise ItemEditRejected("Строка не найдена в этом чеке.")
        if item.is_returned:
            raise ItemEditRejected(
                "Строка возвращена клиентом — её состав уже не про этот заказ."
            )

        if receipt.stock_deducted:
            _deduct_stock_for_item(item, user, restore=True)

        if change.get("remove"):
            item.delete()
            continue

        qty = change.get("quantity")
        if qty is not None:
            qty = Decimal(str(qty))
            if qty <= 0:
                raise ItemEditRejected(
                    "Количество должно быть больше нуля. Ноль — это удаление строки."
                )
            item.quantity = qty
        price = change.get("price_per_item")
        if price is not None:
            price = Decimal(str(price))
            if price < 0:
                raise ItemEditRejected("Цена не может быть отрицательной.")
            item.price_per_item = price
        item.save(update_fields=["quantity", "price_per_item"])

        if receipt.stock_deducted:
            # Хватит ли остатка на увеличенное количество. У рулонных это ловит
            # FIFO сам, а у штучных проверки не было нигде: `apply_stock_change`
            # спокойно уводит остаток в минус. В кассе это прикрыто тем, что
            # карточка «нет в наличии» не нажимается, а правка чека такой защиты
            # не имеет — без явной проверки опечатка «100000 штук» тихо сделала
            # бы склад отрицательным.
            if item.type == TransactionItem.Type.MATERIAL and item.material_id:
                material = Material.objects.get(pk=item.material_id)
                if not material.is_roll_material:
                    need = item.quantity
                    if item.sale_mode == TransactionItem.SaleMode.PIECE and material.piece_area:
                        need = material.piece_area * item.quantity
                    if need > material.quantity:
                        raise ItemEditRejected(
                            f"На складе только {material.quantity} "
                            f"{material.get_unit_display()} «{material.name}» — "
                            f"на {need} не хватит."
                        )
            # Списываем заново — уже по исправленному количеству. Не хватило —
            # InsufficientStock, и транзакция целиком откатывается (правка не
            # проходит частично).
            _deduct_stock_for_item(item, user, restore=False)

    # --- Журнал склада: оставляем по одной записи продажи на строку ---------
    fresh = list(
        receipt.inventory_logs.exclude(id__in=log_ids_before).values_list(
            "id", "type", "material_id"
        )
    )
    InventoryLog.objects.filter(
        id__in=[lid for lid, kind, _ in fresh if kind == InventoryLog.Type.RETURN]
    ).delete()
    # По каждому материалу убираем столько СТАРЫХ продаж, сколько создали новых:
    # так журнал сходится и когда один и тот же материал стоит в чеке дважды.
    new_sales = Counter(
        mat for _, kind, mat in fresh if kind == InventoryLog.Type.SALE
    )
    stale = []
    for material_id, count in new_sales.items():
        stale += [
            lid for lid, mat in sale_before if mat == material_id
        ][:count]
    InventoryLog.objects.filter(id__in=stale).delete()

    _resettle(receipt, held=held)
    return receipt


@transaction.atomic
def give_change(receipt: Receipt, amount=None, *, user=None) -> Decimal:
    """Выдать клиенту сдачу — целиком или часть. Возвращает выданную сумму.

    Зеркало приёма оплаты: там деньги пришли, тут ушли. Частичная выдача нужна
    потому, что мелочи в кассе может не хватить и во второй раз тоже — «отдал
    тысячу из полутора» это нормальная ситуация цеха, а не ошибка.
    """
    due = receipt.change_due
    if due <= 0:
        raise PaymentRejected("По этому заказу сдачи нет.")
    give = due if amount is None else min(amount, due)
    if give <= 0:
        raise PaymentRejected("Сумма выдачи должна быть больше нуля.")
    receipt.change_due = due - give
    receipt.save(update_fields=["change_due", "updated_at"])
    cash.change_given(receipt, give, user=user)
    return give


def receipt_summary(receipt: Receipt) -> str:
    """Короткое описание чека одной строкой — для журнала действий.

    Пишется ПЕРЕД удалением: после него от чека не остаётся ничего, и вопрос
    «что там было» отвечать будет нечем.
    """
    head = f"№{receipt.order_number}" if receipt.order_number else "без номера"
    if receipt.title:
        head += f" «{receipt.title}»"
    if receipt.client_id:
        head += f", клиент {receipt.client.display_name}"
    lines = ", ".join(
        f"{i.material.name if i.material_id else i.service.name} × {i.quantity}"
        for i in receipt.items.all()
    )
    return f"{head}, {receipt.total_price} сом ({lines})" if lines else f"{head}, {receipt.total_price} сом"


@transaction.atomic
def delete_receipt(receipt: Receipt, *, user=None) -> None:
    """Удалить ошибочно заведённый чек целиком, вернув материал на склад.

    Возврат и удаление — разные вещи, и обе нужны. ВОЗВРАТ — это событие
    business-жизни: клиент принёс заказ обратно, деньги вернули, в отчётах он
    обязан остаться. УДАЛЕНИЕ — исправление опечатки: такого заказа не было
    вовсе. Поэтому здесь мы не оставляем «возврат», а убираем след целиком:

    - количество материала возвращается на склад (рулонные — в те же партии
      FIFO, откуда ушли);
    - записи журнала движений по этому чеку удаляются обе — и расход, и только
      что сделанный возврат: показывать «продажа по чеку №18 / возврат по чеку
      №18» для заказа, которого нет, — врать журналу, а «проданные» в складском
      листе считались бы по несуществующей продаже;
    - оплаты снимаются вместе с чеком (CASCADE).

    След остаётся в ЖУРНАЛЕ ДЕЙСТВИЙ — кто, когда и что удалил, вместе с
    составом (см. ``receipt_summary``). Это ответственность администратора, у
    складовщика такой кнопки нет.
    """
    # Сдача, зачтённая в этот заказ, возвращается клиенту: заказа не было,
    # значит и тратить её было не на что. Кладём на самый свежий из его
    # оставшихся заказов — выдают сдачу по заказу, и для выдачи важна сумма, а
    # не то, на какой строке она числится. Не осталось ни одного заказа —
    # возвращать некуда, и это видно в журнале действий вместе с удалением.
    if receipt.change_applied > 0 and receipt.client_id:
        host = (
            Receipt.objects.filter(client_id=receipt.client_id)
            .exclude(pk=receipt.pk)
            .order_by("-created_at", "-id")
            .first()
        )
        if host:
            host.change_due += receipt.change_applied
            host.save(update_fields=["change_due", "updated_at"])

    # Возвращаем только НЕвозвращённые строки: по возвращённым материал уже
    # вернулся на склад при возврате, второй раз его класть нельзя.
    #
    # И только если он вообще уходил. Неоплаченный онлайн-заказ склад не трогает
    # — а удаление всё равно «возвращало» его позиции, и остаток рос из ничего:
    # брошенный счёт на 2 кв.м поднимал склад на 2 кв.м и стоимость склада на
    # полторы тысячи, сколько бы раз это ни повторили. Именно такие висящие
    # счета админ и вычищает пачками.
    if _stock_was_deducted(receipt):
        for item in receipt.items.filter(is_returned=False):
            _deduct_stock_for_item(item, user, restore=True)
    # Логи (и продажи, и возвраты, и только что сделанное восстановление) — все
    # ссылаются на этот чек, поэтому уходят одним запросом.
    receipt.inventory_logs.all().delete()
    receipt.delete()


@transaction.atomic
def refund_receipt(receipt: Receipt, *, item_ids=None, user=None) -> Receipt:
    """Refund the whole receipt or specific line items, returning stock.

    Returns deducted materials back to the warehouse and updates statuses.
    """
    items = receipt.items.filter(is_returned=False)
    if item_ids:
        items = items.filter(id__in=item_ids)
    # Возврат по уже возвращённому заказу раньше отвечал «успешно», хотя не
    # делал ничего: цикл проходил по пустому списку. Молчаливое «ок» на пустой
    # операции — худший ответ: кассир уверен, что деньги ушли второй раз.
    if not items.exists():
        raise ItemEditRejected("Возвращать нечего: эти позиции уже возвращены.")

    # Stock was only deducted if the receipt was actually settled.
    stock_was_deducted = _stock_was_deducted(receipt)

    # Сколько клиент переплатил относительно того, что у него ОСТАЁТСЯ на руках.
    # До возврата и после: разница — это и есть деньги, которые ему отдают.
    # Прежнее `min(возврат, оплачено)` при частичной оплате и двух возвратах
    # подряд отдавало больше, чем принимали (оплачено 300 из 452, вернули 200 и
    # ещё 200 → выдало бы 400).
    def _excess():
        return max(receipt.amount_paid - (receipt.total_price - receipt.refunded_amount), Decimal("0"))

    excess_before = _excess()
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
            # Ровно то, что стояло в чеке за эту строку — вверх до сома, как
            # `line_total`. Сырое qty × price давало 147.60 против 148 в чеке, и
            # полностью возвращённый заказ оставлял «долг» в копейки.
            refunded_total += item.line_total
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
    # Из кассы уходит ровно переплата, возникшая этим возвратом: неоплаченный
    # заказ возврата денег не порождает вовсе, оплаченный целиком — вернёт
    # стоимость возвращённых строк, оплаченный частично — только то, что
    # выходит за стоимость оставшихся.
    cash.refund_paid(receipt, _excess() - excess_before, user=user)
    return receipt
