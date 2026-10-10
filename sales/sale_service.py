"""Core sales business logic: build a receipt, deduct stock, handle payment
confirmation and refunds. Kept separate from the views so it can be reused by
the payment webhook and tested in isolation.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.utils import timezone

from finance import cash
from warehouse.models import InventoryLog, Material
from warehouse.rolls import (
    consume_area,
    consume_metres,
    has_lots,
    restore_area,
    restore_metres,
)
from warehouse.stock import apply_stock_change

from .models import Payment, Receipt, TransactionItem, TransactionItemLot
from .pricing_rules import LineRules, price_for


def _money(value: Decimal) -> Decimal:
    """До копеек. Без этого SQLite сохранил бы «сырой» результат умножения, а
    PostgreSQL округлил бы его сам — и цифры на dev и на проде разошлись бы."""
    return Decimal(value).quantize(Decimal("0.01"))


@transaction.atomic
def lock_receipt(receipt: Receipt) -> Receipt:
    """Взять чек под замок и ПЕРЕЧИТАТЬ его — на объекте вызывающего.

    Вьюха читает чек до транзакции, и два одновременных запроса на один чек
    видели одно и то же «долг есть / сдача есть / не возвращено» — и оба
    проводили операцию (8 параллельных `/refund/` давали 8 записей REFUND в
    кассе). Теперь любая операция над деньгами и составом чека начинается с
    замка на его строку и свежего состояния: второй запрос ждёт первого и
    видит уже его результат.

    Объект обновляется на месте — вызывающий продолжает работать с ним же (и
    его `refresh_from_db`/prefetch не разъезжается с базой). Чек удалили, пока
    запрос ждал, — 404, а не 500. Только внутри транзакции: на PostgreSQL
    `select_for_update` вне неё падает.
    """
    try:
        Receipt.objects.select_for_update().get(pk=receipt.pk)
    except Receipt.DoesNotExist:
        raise Http404("Заказ уже удалён.")
    receipt.refresh_from_db()
    # Кэш строк и оплат, подтянутый вьюхой заранее, после замка устарел.
    getattr(receipt, "_prefetched_objects_cache", {}).clear()
    return receipt


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
            preferred_roll=None, trace=None) -> Decimal:
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
    # ПАРТИИ теперь бывают и у штучного материала (2026-08-27): приход заводит
    # партию со своей ценой за штуку, касса даёт выбрать, из какой берём.
    # Материал БЕЗ партий (заведён до этой правки, остаток поднимали руками)
    # работает как раньше — иначе старый запас нельзя было бы продать вовсе.
    if material.is_roll_material or has_lots(material):
        # FIFO знает, из каких именно партий ушёл материал и почём.
        return consume_area(
            material, qty, user=user, reason=reason,
            log_type=InventoryLog.Type.SALE, receipt=receipt, happened_at=happened_at,
            preferred_roll=preferred_roll, trace=trace,
        )
    apply_stock_change(
        material, -qty, user=user, reason=reason,
        log_type=InventoryLog.Type.SALE, receipt=receipt, happened_at=happened_at,
    )
    # Партий нет — берём текущую закупочную цену из карточки.
    return qty * (material.purchase_price or Decimal("0"))


def _restore(material, qty, user, reason="", receipt=None, happened_at=None,
             preferred_roll=None, lots=None) -> Decimal:
    """Вернуть материал на склад при возврате заказа.

    Тип ВОЗВРАТ, а не «корректировка»: корректировка — это инвентаризация, а
    здесь у прихода есть парный расход по тому же чеку.
    """
    if qty <= 0:
        return Decimal("0")
    # Возврат идёт тем же путём, что и списание: в партию, из которой брали.
    # Иначе штучный возврат поднял бы только число остатка, и партии стали бы
    # знать меньше материала, чем лежит на полке.
    # Строка помнит свои партии — возвращаем в них, даже если сейчас они пусты
    # (продали всё подчистую): `has_lots` видит только непустые.
    if material.is_roll_material or has_lots(material) or (
        lots and material.rolls.filter(pk__in=[pk for pk, *_ in lots]).exists()
    ):
        restore_area(
            material, qty, user=user, reason=reason,
            log_type=InventoryLog.Type.RETURN, receipt=receipt,
            preferred_roll=preferred_roll, lots=lots,
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

    Условие держит и правку состава, и возврат, и удаление чека, и дозаказ.

    Для ОНЛАЙН-счёта решает один только флаг `stock_deducted`. Раньше сюда же
    примешивался статус оплаты («Оплачено» / «Частичный возврат»), но у
    неоплаченного онлайн-счёта с возвращённой строкой статус как раз «Частичный
    возврат» при несписанном складе: следующий возврат или удаление клали на
    полку то, чего оттуда не брали, а дозаказ списывал сразу и второй раз —
    при оплате.

    Наличный чек без флага — старые данные: его склад уходил при оформлении
    всегда, поэтому для него подстраховка по статусу оплаты остаётся.
    """
    if receipt.stock_deducted:
        return True
    if receipt.payment_method == Receipt.PaymentMethod.ONLINE:
        return False
    return receipt.payment_status in (
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

    У ОТХОДОВ мерка своя в каждой строке: квадраты — это площадь, а метры и
    штуки площадью не являются вовсе. Считать их квадратами значило бы списать
    расходник техкарты по чужой мерке.
    """
    if item.service_id and item.service.uses_running_meter:
        if item.width and item.length:
            return _area(item.width, item.length)
        return Decimal("0")
    if item.service_id and item.service.uses_free_measure:
        if item.sale_mode != TransactionItem.SaleMode.SQM:
            return Decimal("0")
        if item.width and item.length:
            return _area(item.width, item.length)
        return item.quantity
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


def _cost_warning(item: TransactionItem, material: Material) -> dict:
    """Предупреждение «себестоимость неизвестна» для ответа и журнала действий."""
    return {
        "code": "cost_unknown",
        "item": item.id,
        "material": material.id,
        "material_name": material.name,
        "message": (
            f"«{material.name}»: себестоимость неизвестна — у материала нет партий "
            "и закупочной цены, строка уйдёт в учёт с нулевой себестоимостью, "
            "а маржа по ней будет завышена."
        ),
    }


def _save_lot_uses(item: TransactionItem, trace) -> None:
    """Запомнить, из каких партий и сколько взято на эту строку.

    Возврат кладёт материал туда же, откуда взяли (`_restore_lots`). Без записи
    возврат шёл «в первую партию по FIFO» и перекладывал остаток дорогой партии
    в дешёвую: продажа из двух партий (1000 + 3000) после возврата оставляла
    склад в 2000 и продавала дальше по 1000.
    """
    item.lot_uses.all().delete()
    if trace:
        TransactionItemLot.objects.bulk_create(
            TransactionItemLot(item=item, roll_id=pk, area=area, metres=metres)
            for pk, area, metres in trace
        )


def _restore_lots(item: TransactionItem):
    """Партии, из которых строку списали, — для `restore_*`. Пусто у старых строк
    (до учёта партий по строке): тогда возврат идёт как раньше."""
    return [
        (use.roll_id, use.area, use.metres)
        for use in item.lot_uses.order_by("id")
        if use.roll_id
    ]


def _deduct_stock_for_item(item: TransactionItem, user, *, restore=False) -> list:
    """Deduct (or restore) stock for a single line item.

    Возвращает предупреждения (`_cost_warning`) — пустой список, если всё хорошо.

    Движения журнала склада, которые породила строка, привязываются к ней
    (`InventoryLog.receipt_item`): правка состава чека должна сторнировать
    именно записи ЭТОЙ строки, а не «первую продажу того же материала».
    """
    receipt = item.receipt
    # Чек под замком (его держат все вызывающие), поэтому всё, что появилось в
    # его журнале после этой точки, породила именно эта строка.
    last_log = (
        InventoryLog.objects.filter(receipt=receipt).aggregate(m=Max("id"))["m"] or 0
    )
    warnings = _move_stock_for_item(item, user, restore=restore)
    InventoryLog.objects.filter(receipt=receipt, id__gt=last_log).update(receipt_item=item)
    if warnings:
        from audit.models import AuditLog

        number = receipt.order_number or receipt.pk
        for warning in warnings:
            AuditLog.record(user, f"Чек {number}: {warning['message']}")
    return warnings


def _move_stock_for_item(item: TransactionItem, user, *, restore=False) -> list:
    """Deduct (or restore) stock for a single line item.

    Cutting now produces two separate lines (a MATERIAL line for the cut material
    and a SERVICE line for the master's work), so the MATERIAL line handles its
    own area; service lines only consume their recipe (technological-card) extras.

    Каждое движение попадает в складской журнал со ссылкой на чек — и продажа
    материала, и расход по техкарте услуги (клей, крепёж).
    """
    fn = _restore if restore else _deduct
    receipt = item.receipt
    warnings = []
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
                    .order_by("received_at", "pk")
                    .first()
                )
                if first is not None:
                    item.roll = first
                    item.save(update_fields=["roll"])
            metre_fn = restore_metres if restore else consume_metres
            trace = [] if not restore else None
            lot_args = {"lots": _restore_lots(item)} if restore else {"trace": trace}
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
                **lot_args,
                **extra,
            )
            if not restore:
                item.cost_total = _money(cost or Decimal("0"))
                item.save(update_fields=["cost_total"])
                _save_lot_uses(item, trace)
                if not item.cost_total:
                    warnings.append(_cost_warning(item, item.material))
            else:
                item.lot_uses.all().delete()
                _unarchive_returned(item.material, receipt, user)
            return warnings
        # Whole-piece sales deduct the piece area; area/qty sales deduct quantity.
        qty = item.quantity
        if item.sale_mode == TransactionItem.SaleMode.PIECE and item.material.piece_area:
            qty = item.material.piece_area * item.quantity
        # У ЛИСТА партии тоже есть, и мастер может взять лист из конкретной
        # пачки: партия строки чека уходит в списание первой, остальные — за
        # ней обычным FIFO. Партию запоминаем в строке (как у рулона), иначе
        # возврат вернул бы листы не в ту пачку, а себестоимость строки
        # перестала бы сходиться с той, по которой продали.
        # Со штучным материалом ровно та же история (2026-08-27): у него теперь
        # тоже бывают партии по разной цене, и выбранная в кассе обязана дойти
        # до списания. Раньше проверка стояла по флагу «площадной», и партия
        # штучной строки молча терялась: FIFO брал старейшую, а себестоимость
        # выходила не та, что показали при продаже.
        trace = None
        if restore:
            extra = {**extra, "lots": _restore_lots(item)}
        if item.material.is_roll_material or has_lots(item.material):
            if not restore and item.roll_id is None:
                from warehouse.models import Roll

                first = (
                    Roll.objects.filter(material=item.material, remaining_area__gt=0)
                    .order_by("received_at", "pk")
                    .first()
                )
                if first is not None:
                    item.roll = first
                    item.save(update_fields=["roll"])
            extra = {**extra, "preferred_roll": item.roll_id}
            if not restore:
                trace = extra["trace"] = []
        cost = fn(
            item.material, qty, user,
            reason=_reason(receipt, restore=restore), receipt=receipt, **extra,
        )
        if not restore:
            item.cost_total = _money(cost)
            item.save(update_fields=["cost_total"])
            _save_lot_uses(item, trace)
            if qty > 0 and not item.cost_total:
                warnings.append(_cost_warning(item, item.material))
        else:
            item.lot_uses.all().delete()
            _unarchive_returned(item.material, receipt, user)
        return warnings
    if item.type != TransactionItem.Type.SERVICE or not item.service_id:
        return warnings

    # Extra recipe materials (e.g. fasteners for installation, glue, …) — их
    # себестоимость тоже относим на строку услуги.
    cost = Decimal("0")
    reason = _reason(receipt, restore=restore, service=item.service)
    trace = []
    lots = _restore_lots(item) if restore else None
    for recipe in item.service.recipes.select_related("material").all():
        consumed = recipe_consumption(recipe, item)
        if restore:
            part = fn(recipe.material, consumed, user, reason=reason, receipt=receipt,
                      lots=lots, **extra)
        else:
            part = fn(recipe.material, consumed, user, reason=reason, receipt=receipt,
                      trace=trace, **extra)
            if consumed > 0 and not part:
                warnings.append(_cost_warning(item, recipe.material))
        cost += part
        if restore:
            # Расходники техкарты возвращаются той же логикой, что и материал
            # строки: спрятанный клей после возврата тоже снова на складе.
            _unarchive_returned(recipe.material, receipt, user)
    if restore:
        item.lot_uses.all().delete()
    else:
        _save_lot_uses(item, trace)
        if cost:
            item.cost_total = _money(cost)
            item.save(update_fields=["cost_total"])
    return warnings


def _line_rules(receipt: Receipt, item_type, service) -> LineRules:
    """Правила прайса для новой строки этого чека.

    Срочность и скидка — заказа (записаны на чеке при оформлении: дозаказ
    считается по ним же). Минимум — только у строк услуг: своя сумма услуги,
    если задана (0 — без минимума), иначе общая из настроек цен. Материал
    минимумом не облагается: лист, крепёж или кусок под рез продаются по
    своей цене, иначе один саморез стоил бы как работа.
    """
    minimum = Decimal("0")
    if item_type == TransactionItem.Type.SERVICE and service is not None:
        if service.min_line_amount is not None:
            minimum = service.min_line_amount
        else:
            cached = getattr(receipt, "_global_min_line", None)
            if cached is None:
                from services.models import PricingSettings

                cached = PricingSettings.load().min_line_amount
                receipt._global_min_line = cached
            minimum = cached
    urgency = receipt.urgency_percent if receipt.is_urgent else Decimal("0")
    return LineRules(
        minimum=minimum or Decimal("0"),
        urgency=urgency or Decimal("0"),
        discount=receipt.discount_percent or Decimal("0"),
    )


def _create_line(receipt: Receipt, **fields) -> TransactionItem:
    """Создать строку чека, применив правила прайса к цене за единицу.

    `price_per_item` в `fields` — цена ДО правил (каталожная или вписанная);
    она запоминается в `catalog_price`, а в `price_per_item` уходит цена после
    правил — та, по которой строка стоит в чеке и в отчётах.
    """
    rules = _line_rules(receipt, fields.get("type"), fields.get("service"))
    base = Decimal(fields.pop("price_per_item"))
    price, min_applied = price_for(Decimal(fields["quantity"]), base, rules)
    return TransactionItem.objects.create(
        receipt=receipt,
        price_per_item=price,
        catalog_price=base,
        min_amount=rules.minimum if rules.minimum > 0 else None,
        min_applied=min_applied,
        urgency_percent=rules.urgency,
        discount_percent=rules.discount,
        **fields,
    )


def reprice_line(item: TransactionItem, *, base_price=None) -> None:
    """Пересчитать цену строки по ЕЁ правилам после правки количества/цены.

    Правила — записанные на строке при продаже (минимум, срочность, скидка
    заказа), а не сегодняшние: правка опечатки не должна менять условия
    сделки. `base_price` — новая цена до правил (правка цены админом). Строка,
    проданная до правил, не трогается — её цена и есть её цена.
    """
    if item.catalog_price is None:
        if base_price is not None:
            item.price_per_item = base_price
        return
    if base_price is not None:
        item.catalog_price = base_price
    rules = LineRules(
        minimum=item.min_amount or Decimal("0"),
        urgency=item.urgency_percent or Decimal("0"),
        discount=item.discount_percent or Decimal("0"),
    )
    item.price_per_item, item.min_applied = price_for(item.quantity, item.catalog_price, rules)


def _line_name(item: TransactionItem) -> str:
    if item.material_id:
        return item.material.name
    return item.service.name if item.service_id else "—"


def below_cost_warnings(items, user=None) -> list:
    """Предупреждения «строка продана ниже себестоимости» (CALC-08).

    Продажу не блокируют — как и «себестоимость неизвестна» (D-49): скидку
    или ручную цену ниже закупки владелец может дать осознанно. Но кассир и
    владелец должны это видеть, и запись остаётся в журнале действий.
    Себестоимость в ответе — только для тех, кто видит деньги (это решает
    вьюха, `strip_cost`).
    """
    warnings = []
    for item in items:
        if item.is_returned or item.cost_total <= 0:
            continue
        if item.sold_total >= item.cost_total:
            continue
        name = _line_name(item)
        warnings.append({
            "code": "below_cost",
            "item": item.id,
            "name": name,
            "line_total": item.sold_total,
            "cost_total": item.cost_total,
            "message": f"«{name}»: строка продана за {item.sold_total} сом — ниже себестоимости.",
        })
    if warnings:
        from audit.models import AuditLog

        receipt = items[0].receipt if items else None
        number = (receipt.order_number or receipt.pk) if receipt else "—"
        for w in warnings:
            AuditLog.record(
                user, f"Чек {number}: {w['message']} Себестоимость {w['cost_total']} сом."
            )
    return warnings


def strip_cost(warnings, user) -> list:
    """Себестоимость в предупреждениях — только тем, кто видит деньги."""
    if getattr(user, "sees_money", False):
        return warnings
    return [{k: v for k, v in w.items() if k != "cost_total"} for w in warnings]


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
    - SERVICE / WASTE (отходы): мерка из `mode` — кв.м, пог.м или штуки;
      склада не касается, материала отдельной строкой нет.
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
        return [_create_line(
            receipt, type=item_type, material=material,
            quantity=qty, price_per_item=price,
            sale_mode=mode,
            # Партию запоминаем на строке: из неё списывали, в неё же вернём
            # при возврате, и по ней в чеке пишется «списано с рулона №7».
            # Не только у рулона: у листа пачки тоже разные по цене закупки, и
            # мастер может взять лист из той, что стоит ближе. С 2026-08-27 —
            # и у ШТУЧНОГО: у него тоже бывают поставки по разной цене, и
            # выбранная в кассе партия должна дойти до списания, иначе FIFO
            # молча возьмёт старейшую и себестоимость будет не та.
            roll=entry.get("roll"),
            # Ширина изделия — чтобы посчитать обрезок. Полную ширину списали и
            # деньги за неё взяли; сколько из этого ушло в мусор, без неё не
            # узнать никак.
            used_width=(
                entry.get("used_width") if mode == TransactionItem.SaleMode.METER else None
            ),
        )]

    service = entry["service"]

    # ОТХОДЫ: мерку выбрали в кассе и прислали в `mode` — одна услуга продаёт
    # и квадраты листа, и метры рулона, и штуки. Мерку запоминаем на строке
    # (`sale_mode`), иначе в чеке «Отходы × 2» не отличить 2 кв.м от 2 метров.
    # Материала у строки нет: отход уже списан браком или обрезком резки,
    # второе списание увело бы остаток в минус. Цена — вписанная при продаже
    # (её называет и складовщик), иначе каталожная ДЛЯ ЭТОЙ мерки.
    if service.uses_free_measure:
        mode = entry.get("mode") or TransactionItem.SaleMode.SQM
        width = entry.get("width")
        length = entry.get("length")
        if mode == TransactionItem.SaleMode.SQM and width and length:
            qty = _area(width, length)
        else:
            qty = _qty(entry.get("quantity") or 0)
        catalogue = {
            TransactionItem.SaleMode.METER: service.rate_per_pm,
            TransactionItem.SaleMode.PIECE: service.rate_per_piece,
        }.get(mode, service.rate_flat)
        return [_create_line(
            receipt, type=item_type, service=service,
            quantity=qty, price_per_item=_priced("cut_rate", catalogue),
            sale_mode=mode,
            # Размеры — только у площадной мерки: у метров и штук их нет, и
            # чужие цифры в этих колонках врали бы о том, что мерили.
            width=Decimal(str(width)) if mode == TransactionItem.SaleMode.SQM and width else None,
            length=Decimal(str(length)) if mode == TransactionItem.SaleMode.SQM and length else None,
            note=(entry.get("note") or "")[:255],
        )]

    # Area-priced services: cutting and interior install. Work is computed
    # automatically from the cut area (width × length); no manual entry.
    if service.uses_area:
        width = entry.get("width")
        length = entry.get("length")
        area = _area(width, length) if width and length else _qty(entry.get("quantity") or 0)
        # Материал КЛИЕНТА: со склада ничего не уходит, строки материала нет.
        # Сериализатор материал при этом флаге не пропускает; здесь — на случай
        # прямого вызова, чтобы «чужой» лист точно не списался.
        own_material = bool(entry.get("own_material"))
        material = None if own_material else entry.get("material")

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
        work = _create_line(
            receipt, type=TransactionItem.Type.SERVICE, service=service,
            quantity=work_qty, price_per_item=rate,
            width=Decimal(str(width)) if width else None,
            length=Decimal(str(length)) if length else None,
            own_material=own_material,
            note=(entry.get("note") or "")[:255],
        )
        items = [work]
        # The cut/used material is billed as its own line (area × per-кв.м price,
        # or a manual per-кв.м override entered at sale time). Cutting work on a
        # whole sheet has no cut dimensions (area=0) — the sheet is billed
        # separately as a PIECE line, so we bill only the work here.
        if service.uses_material and material and area > 0:
            items.append(_create_line(
                receipt, type=TransactionItem.Type.MATERIAL, material=material,
                quantity=area, price_per_item=_priced("material_price", material.sqm_price),
                sale_mode=TransactionItem.SaleMode.SQM,
                # Режут из ВЫБРАННОЙ пачки — как и при обычной продаже листа.
                roll=entry.get("roll") if material.is_roll_material else None,
            ))
        return items

    # Per-piece service: exterior install (price per letter × count).
    if service.uses_pieces:
        return [_create_line(
            receipt, type=item_type, service=service,
            quantity=Decimal(entry.get("quantity") or 1), price_per_item=service.rate_per_piece,
            note=(entry.get("note") or "")[:255],
        )]

    # FIXED-price service (legacy installation / other)
    return [_create_line(
        receipt, type=item_type, service=service,
        quantity=Decimal(entry.get("quantity") or 1), price_per_item=service.base_price,
        note=(entry.get("note") or "")[:255],
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
    # Замок на все чеки клиента в порядке id — тот же, что в `pay_client_debt`.
    list(Receipt.objects.select_for_update().filter(client=client).order_by("pk"))
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
    is_urgent=False, urgency_percent=None, discount_percent=None,
) -> Receipt:
    """Create a receipt with its line items.

    ``is_urgent`` / ``urgency_percent`` / ``discount_percent`` — правила прайса
    заказа (`sales.pricing_rules`): записываются на чек и применяются к
    каждой строке. Проценты уже проверены вьюхой (права, диапазон);
    ``urgency_percent`` не задан — берётся из настроек цен.

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
        is_urgent=bool(is_urgent),
        urgency_percent=(
            _order_urgency(urgency_percent) if is_urgent else Decimal("0")
        ),
        discount_percent=discount_percent or Decimal("0"),
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
        receipt.cost_warnings = _deduct_all(receipt)
        receipt.cost_warnings += below_cost_warnings(
            list(receipt.items.filter(is_returned=False).select_related("material", "service")),
            cashier,
        )
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


def _order_urgency(percent=None) -> Decimal:
    """Наценка за срочность для нового заказа: явная или из настроек цен."""
    if percent is not None:
        return Decimal(percent)
    from services.models import PricingSettings

    return PricingSettings.load().urgency_percent


def _deduct_all(receipt: Receipt) -> list:
    """Deduct stock for every line item of the receipt.

    ВОЗВРАЩЁННЫЕ строки пропускаем: их вернули ещё до списания (неоплаченный
    онлайн-счёт склад не трогал), и списать их сейчас — значит увести со склада
    то, что клиент давно не покупал. Возвращает предупреждения о строках без
    известной себестоимости.
    """
    warnings = []
    for item in receipt.items.filter(is_returned=False):
        warnings += _deduct_stock_for_item(item, receipt.cashier)
    return warnings


def _settle(receipt: Receipt, received: Decimal) -> None:
    """Принять оплату шлюза: чек оплачен, склад списан (один раз).

    ``received`` — сколько реально пришло от шлюза. Оплаченное считаем не
    «итог чека», а ВСЕ деньги, что лежат по чеку: раньше `amount_paid`
    затирался итогом, и 400, уже принятые в кассе, исчезали из чека, оставаясь
    в книге (в кассе 1400 при заказе на 1000, и ничто не объясняет лишние 400).
    Не больше долга чека (итог минус возврат) идёт в `amount_paid`, остальное —
    сдача клиенту: деньги у цеха, но заказ они не оплачивают.
    """
    if not receipt.stock_deducted:
        receipt.cost_warnings = _deduct_all(receipt)
        receipt.stock_deducted = True
    money_in = receipt.amount_paid + received
    due = max(receipt.total_price - receipt.refunded_amount, Decimal("0"))
    receipt.amount_paid = min(money_in, due)
    receipt.change_due = receipt.change_due + (money_in - receipt.amount_paid)
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
    lock_receipt(receipt)
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
    receipt.cost_warnings = []
    built = []
    for entry in items_data:
        for item in _build_item(receipt, entry):
            surcharge += item.line_total
            built.append(item)
            if deduct_now:
                receipt.cost_warnings += _deduct_stock_for_item(item, user)
    if deduct_now:
        receipt.cost_warnings += below_cost_warnings(built, user)

    receipt.recalculate_total()
    if (
        receipt.payment_status == Receipt.PaymentStatus.PAID
        and receipt.amount_paid < receipt.total_price - receipt.refunded_amount
    ):
        receipt.payment_status = Receipt.PaymentStatus.PENDING
    receipt.save(update_fields=["total_price", "amount_paid", "payment_status", "updated_at"])
    return receipt, surcharge


def recognize_online_sale(receipt: Receipt) -> None:
    """Онлайн-заказ стал продажей: оплату подтвердили (2026-10-07, D-7/D-14).

    До этого неоплаченный онлайн-счёт — не выручка и не долг. Признаём его
    моментом подтверждения и в тот же момент списываем склад: себестоимость
    проданного ложится в тот же период, что и выручка. Раньше выручка стояла
    в месяце заказа, а себестоимость появлялась там же «задним числом» в день
    оплаты — даже если месяц уже закрыт.

    Дату заказа не трогаем: её видел клиент. Повторный вызов ничего не делает.
    Сохраняет чек сам (`revenue_recognized_at`, `stock_deducted`).
    """
    if receipt.payment_method != Receipt.PaymentMethod.ONLINE or receipt.revenue_recognized_at:
        return
    if not receipt.stock_deducted:
        _deduct_all(receipt)
        receipt.stock_deducted = True
    receipt.revenue_recognized_at = timezone.now()
    receipt.save(update_fields=["revenue_recognized_at", "stock_deducted", "updated_at"])


@transaction.atomic
def confirm_payment(receipt: Receipt) -> Receipt:
    """Called when the payment gateway confirms an online payment."""
    lock_receipt(receipt)
    if receipt.payment_status == Receipt.PaymentStatus.PAID:
        return receipt
    # Счёт уже отменён (возвращён целиком), а шлюз всё равно взял деньги. Заказ
    # из мёртвых не поднимаем: склад не списываем, выручку не признаём, чек
    # остаётся отменённым. Деньги при этом у шлюза есть — это должен увидеть
    # владелец, чтобы вернуть их клиенту руками.
    if (
        receipt.status == Receipt.Status.CANCELLED
        or receipt.payment_status == Receipt.PaymentStatus.REFUNDED
    ):
        from audit.models import AuditLog

        AuditLog.record(
            receipt.cashier,
            f"Шлюз подтвердил оплату {receipt.total_price} сом по отменённому онлайн-счёту "
            f"№{receipt.order_number or receipt.pk}: заказ не восстановлен, "
            "деньги нужно вернуть клиенту вручную",
        )
        return receipt
    # Шлюз платит по счёту, выставленному на итог чека.
    received = receipt.total_price
    _settle(receipt, received)
    receipt.save(update_fields=[
        "payment_status", "amount_paid", "change_due", "stock_deducted", "updated_at",
    ])
    recognize_online_sale(receipt)
    # Онлайн-оплата — такой же приход денег, как наличные в ящик и перевод на
    # карту, и в кассовую книгу она обязана попасть. Этой строки тут не было:
    # приход писали только `create_sale` и `apply_payment`, а онлайн-заказ шёл
    # мимо обоих. Чек становился «Оплачено», выручка в отчёте росла, а «Касса и
    # банк» этих денег не видела вовсе — свести остаток по счёту было нечем.
    #
    # Счёт выбирает `cash.account_for` по способу оплаты: ONLINE это не
    # наличные, значит банк. Дата — сегодняшняя (день подтверждения оплаты, а не
    # оформления заказа): деньги приходят именно тогда, когда их подтвердил шлюз.
    # В книгу идёт то, что пришло от шлюза, — деньги, принятые в кассе раньше,
    # там уже записаны своим приходом.
    #
    # Идемпотентность держит проверка в начале функции (под замком чека):
    # повторное подтверждение того же чека выходит раньше и второй записи не
    # делает.
    cash.receipt_paid(receipt, received, user=receipt.cashier)
    return receipt


class PaymentRejected(Exception):
    """Оплату принять нельзя. Текст исключения уходит пользователю как есть."""


def normalize_method(raw):
    """'cash' → 'CASH'; пусто → None («как у чека»); незнакомое → отказ.

    Раньше способ шёл в запись как есть: «cash» строчными не совпадал ни с
    одним значением и молча уходил в банк (`cash.account_for` проверяет только
    точное «CASH»), а в истории оплат оставалась запись с несуществующим
    способом.
    """
    if raw in (None, ""):
        return None
    value = str(raw).strip().upper()
    if value not in Receipt.PaymentMethod.values:
        allowed = ", ".join(Receipt.PaymentMethod.values)
        raise PaymentRejected(f"Неизвестный способ оплаты. Ожидается один из: {allowed}.")
    return value


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
    # Замок и свежее состояние ДО проверок: два одновременных запроса на один
    # долг оба видели «долг есть» и оба проводили оплату (8 × `/pay/` по 100 —
    # восемь Payment и +800 в кассе при `amount_paid` 100).
    lock_receipt(receipt)
    method = normalize_method(method)
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
    # Онлайн-счёт, оплаченный не шлюзом, а в кассе (`/pay/`), — та же продажа,
    # что и подтверждённый шлюзом: признаём и списываем склад. Раньше такой чек
    # становился «Оплачено», а материал со склада не уходил вовсе.
    if receipt.payment_status == Receipt.PaymentStatus.PAID:
        recognize_online_sale(receipt)

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
    # днём, когда деньги реально принесли. В ящик легло ВСЁ принесённое, вместе
    # со сдачей: раньше приходовалась только часть в зачёт долга, а выдача
    # сдачи потом уводила кассу ниже ящика на её сумму.
    cash.receipt_paid(
        receipt, amount + over, user=user, happened_on=settled_on,
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
    method = normalize_method(method)
    wanted = {str(x) for x in receipt_ids} if receipt_ids is not None else None
    # Замок на ВСЕ чеки клиента, всегда в одном порядке (по id), и долги
    # считаем уже по свежим строкам: два одновременных погашения не должны
    # видеть один и тот же долг, а разный порядок замков дал бы взаимную
    # блокировку двух кассиров.
    locked = list(
        Receipt.objects.select_for_update().filter(client=client).order_by("pk")
    )
    debts = sorted(
        (
            r
            for r in locked
            if r.debt > 0 and (wanted is None or str(r.id) in wanted)
        ),
        key=lambda r: (r.created_at, str(r.pk)),
    )
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
        # Эти деньги тоже легли в ящик. Раньше в книгу шла только часть,
        # ушедшая в долги: клиент принёс 3 700 за долг 3 200 — касса +3 200,
        # а после выдачи сдачи +2 700 при реальных +3 200.
        cash.receipt_paid(
            last, change, user=user, happened_on=paid_on or timezone.localdate(),
            method=method or last.payment_method,
        )
    return allocations, change


class DeleteRejected(Exception):
    """Чек удалять нельзя (его деньги уже ушли в другие заказы). Текст — клиенту."""


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


def _edit_number(raw, *, places: int, digits: int, what: str) -> Decimal:
    """Число из правки состава: конечное и такой разрядности, как колонка.

    NaN и бесконечность разбираются в `Decimal` без ошибки и роняли запись в
    базу пятисоткой; «4.56789» принималось, на Postgres колонка округляла его
    сама, а склад списывал сырое значение — три разных числа об одной строке.
    """
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise ItemEditRejected(f"{what}: некорректное число.")
    if not value.is_finite():
        raise ItemEditRejected(f"{what}: некорректное число.")
    if -value.normalize().as_tuple().exponent > places:
        raise ItemEditRejected(f"{what}: не больше {places} знаков после запятой.")
    if abs(value) >= Decimal(10) ** (digits - places):
        raise ItemEditRejected(f"{what}: слишком большое число.")
    return value


def _drop_item_journal(receipt: Receipt, item: TransactionItem, linked_ids: set) -> None:
    """Убрать из журнала склада старое списание строки и парный возврат.

    ``linked_ids`` — записи строки ДО возврата. Возврат создал новые (привязаны
    к строке тем же `_deduct_stock_for_item`). У чека, проведённого до привязки
    журнала к строкам, записей строки нет: тогда каждому возврату ищем продажу
    того же материала в том же количестве среди непривязанных записей чека.
    """
    fresh = list(item.inventory_logs.exclude(id__in=linked_ids))
    drop = set(linked_ids)
    drop.update(log.id for log in fresh)
    if not linked_ids:
        taken = set()
        for ret in fresh:
            candidates = [
                log
                for log in InventoryLog.objects.filter(
                    receipt=receipt, type=InventoryLog.Type.SALE,
                    material_id=ret.material_id, receipt_item__isnull=True,
                ).order_by("id")
                if log.id not in taken
            ]
            exact = [log for log in candidates if log.quantity_changed == -ret.quantity_changed]
            match = (exact or candidates or [None])[0]
            if match is not None:
                taken.add(match.id)
                drop.add(match.id)
    InventoryLog.objects.filter(id__in=drop).delete()


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
    lock_receipt(receipt)
    if receipt.status == Receipt.Status.CANCELLED:
        raise ItemEditRejected("Чек отменён — править его состав нельзя.")

    # Деньги на руках — до правки: по чеку с возвратом часть принесённого уже
    # отдали, и считать переплату/долг от полной суммы нельзя (см. `_resettle`).
    held = _money_held(receipt) if receipt.refunded_amount > 0 else None

    for change in changes:
        try:
            item = receipt.items.get(pk=change["id"])
        except (TransactionItem.DoesNotExist, KeyError, ValueError):
            raise ItemEditRejected("Строка не найдена в этом чеке.")
        if item.is_returned:
            raise ItemEditRejected(
                "Строка возвращена клиентом — её состав уже не про этот заказ."
            )

        # Новые значения проверяем ДО того, как тронем склад: NaN, бесконечность
        # и «4.56789» раньше доходили до базы (500 или молчаливое округление на
        # Postgres при сыром списании со склада).
        remove = bool(change.get("remove"))
        qty = price = None
        if not remove:
            if change.get("quantity") is not None:
                qty = _edit_number(
                    change["quantity"], places=3, digits=12, what="Количество"
                )
                if qty <= 0:
                    raise ItemEditRejected(
                        "Количество должно быть больше нуля. Ноль — это удаление строки."
                    )
            if change.get("price_per_item") is not None:
                price = _edit_number(
                    change["price_per_item"], places=2, digits=12, what="Цена"
                )
                if price < 0:
                    raise ItemEditRejected("Цена не может быть отрицательной.")

        if receipt.stock_deducted:
            # Журнал: записи ЭТОЙ строки (старая продажа) и только что созданный
            # возврат убираем — правка опечатки возвратом не называется.
            linked = set(item.inventory_logs.values_list("id", flat=True))
            _deduct_stock_for_item(item, user, restore=True)
            _drop_item_journal(receipt, item, linked)

        if remove:
            item.delete()
            continue

        if qty is not None:
            item.quantity = qty
        # Цена из правки — цена ДО правил прайса (как в кассе): минимум,
        # срочность и скидка строки пересчитываются от неё по тем правилам,
        # что были при продаже. У строки, проданной до правил, — как раньше.
        reprice_line(item, base_price=price)
        item.save(update_fields=["quantity", "price_per_item", "catalog_price", "min_applied"])

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
            receipt.cost_warnings = getattr(receipt, "cost_warnings", []) + (
                _deduct_stock_for_item(item, user, restore=False)
            ) + below_cost_warnings([item], user)

    _resettle(receipt, held=held)
    return receipt


@transaction.atomic
def give_change(receipt: Receipt, amount=None, *, user=None) -> Decimal:
    """Выдать клиенту сдачу — целиком или часть. Возвращает выданную сумму.

    Зеркало приёма оплаты: там деньги пришли, тут ушли. Частичная выдача нужна
    потому, что мелочи в кассе может не хватить и во второй раз тоже — «отдал
    тысячу из полутора» это нормальная ситуация цеха, а не ошибка.
    """
    lock_receipt(receipt)
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


def return_applied_change(receipt: Receipt) -> Decimal:
    """Вернуть клиенту сдачу, зачтённую в этот заказ, — если заказа не было
    (удаление) или денег по нему не брали (откат оплаты).

    Кладём на самый свежий из его ДРУГИХ заказов: выдают сдачу по заказу, и
    для выдачи важна сумма, а не то, на какой строке она числится. Других
    заказов нет — оставляем сдачей на этом же. Деньги при этом не двигаются:
    они лежат в кассе с того раза, когда клиент переплатил.
    """
    applied = receipt.change_applied
    if applied <= 0:
        return Decimal("0")
    host = None
    if receipt.client_id:
        host = (
            Receipt.objects.filter(client_id=receipt.client_id)
            .exclude(pk=receipt.pk)
            .order_by("-created_at", "-id")
            .first()
        )
    receipt.change_applied = Decimal("0")
    if host:
        host.change_due += applied
        host.save(update_fields=["change_due", "updated_at"])
    else:
        receipt.change_due += applied
    receipt.save(update_fields=["change_applied", "change_due", "updated_at"])
    return applied


def _ensure_change_not_spent(receipt: Receipt) -> None:
    """Нельзя удалить чек, чья сдача уже зачтена в ДРУГОЙ заказ клиента.

    Пример аудита: заказ A на 1500, принесли 3000 (сдача 1500 не выдана); сдачу
    зачли в заказ B на 2100 (оплачено 2000, долг 100). Удалили A — касса ушла в
    −3000, а у B осталось «оплачено 2000», хотя 1500 из них — деньги A, которых
    больше нет: реальный долг B 1600.

    Откатить зачёт автоматически нельзя: связь «откуда взяли сдачу — куда
    зачли» нигде не хранится (`_take_client_change` берёт с самых старых заказов
    и пишет лишь `change_applied` на получателе), так что любой откат — догадка
    о чужом долге. Поэтому отказываем и называем заказы, куда она, вероятно,
    ушла: администратор откатывает оплату того заказа (сдача вернётся клиенту) и
    удаляет этот.

    «Сдача потрачена» узнаём по кассе: в ящике по чеку лежит больше, чем сам он
    должен держать (оплачено − зачтено сдачей + невыданная сдача). Чеки с
    возвратами не проверяем — там сверка кассы своя.
    """
    if not receipt.client_id or receipt.refunded_amount > 0:
        return
    held = sum(cash.held_by_account(receipt).values(), Decimal("0"))
    claim = receipt.amount_paid - receipt.change_applied + receipt.change_due
    spent = held - claim
    if spent <= 0:
        return
    targets = list(
        Receipt.objects.filter(client_id=receipt.client_id, change_applied__gt=0)
        .exclude(pk=receipt.pk)
        .order_by("created_at", "pk")
    )
    if not targets:
        return
    numbers = ", ".join(f"№{t.order_number}" for t in targets if t.order_number) or "—"
    raise DeleteRejected(
        f"Из сдачи по этому заказу уже зачтено {spent} сом в другой заказ клиента "
        f"({numbers}). Удалить его сейчас нельзя: у того заказа останется оплата, "
        "которой нет в кассе. Сначала откатите оплату того заказа — сдача "
        "вернётся клиенту, — потом удалите этот."
    )


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
    lock_receipt(receipt)
    _ensure_change_not_spent(receipt)
    # Сдача, зачтённая в этот заказ, возвращается клиенту: заказа не было,
    # значит и тратить её было не на что.
    return_applied_change(receipt)

    # Деньги, которые по этому заказу лежат в кассе, уходят встречной записью,
    # а сами записи остаются в книге (ссылка на чек обнулится): раньше каскад
    # стирал приход целиком, и удалённый оплаченный заказ на 700 молча
    # уменьшал кассу — в книге не оставалось ни строки о том, что деньги были.
    cash.receipt_deleted(receipt, user=user)

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
    # Замок до чтения строк: 8 параллельных возвратов видели одни и те же
    # невозвращённые строки и писали 8 записей REFUND в кассу.
    lock_receipt(receipt)
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
    # Возврат — событие своего дня: отчёты относят его к периоду, когда его
    # оформили, а не к месяцу заказа (см. `sales.reporting`).
    now = timezone.now()
    for item in items:
        # Склад возвращаем, только если он списывался (неоплаченный онлайн-счёт
        # склад не трогал). А вот стоимость строки уходит в `refunded_amount`
        # ВСЕГДА: это не «сколько денег отдали», а «на сколько стало меньше
        # заказа» — из неё считаются долг и выручка ((итог − возвращено) =
        # стоимость оставшихся строк). Раньше у неоплаченного онлайн-заказа
        # возврат строки её сюда не клал, и клиент оставался должен за товар,
        # который вернул. Деньги из кассы уходят отдельно и только сверх
        # принятого (`_excess` ниже): по неоплаченному заказу — ноль.
        if stock_was_deducted:
            _deduct_stock_for_item(item, user, restore=True)
        # Ровно то, что стояло в чеке за эту строку — вверх до сома, как
        # `line_total`. Сырое qty × price давало 147.60 против 148 в чеке, и
        # полностью возвращённый заказ оставлял «долг» в копейки.
        refunded_total += item.line_total
        item.is_returned = True
        item.returned_at = now
        item.save(update_fields=["is_returned", "returned_at"])

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
