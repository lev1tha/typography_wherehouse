"""Core sales business logic: build a receipt, deduct stock, handle payment
confirmation and refunds. Kept separate from the views so it can be reused by
the payment webhook and tested in isolation.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.utils import timezone

from finance import cash
from warehouse.models import InventoryLog, Material
from warehouse.rolls import (
    InsufficientStock,
    consume_area,
    consume_metres,
    has_lots,
    restore_area,
    restore_metres,
)
from services.pricing import apply_passes, resolve_rate
from warehouse.stock import apply_stock_change

from .models import Payment, Receipt, TransactionItem, TransactionItemLot
from .pricing_rules import (
    LineRules,
    allocate_order_total,
    exact_total,
    price_for,
    price_for_target,
)


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
            return _parts_area(item)
        return Decimal("0")
    if item.service_id and item.service.uses_free_measure:
        if item.sale_mode != TransactionItem.SaleMode.SQM:
            return Decimal("0")
        if item.width and item.length:
            return _area(item.width, item.length)
        return item.quantity
    return item.quantity


def _parts_area(item: TransactionItem) -> Decimal:
    """Площадь всех деталей строки: ширина × длина × число деталей, округление
    один раз (как при оформлении)."""
    return _qty(Decimal(str(item.width)) * Decimal(str(item.length)) * (item.parts_count or 1))


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
    if recipe.consumption_mode == ServiceRecipe.Mode.PER_PM:
        # Износ от длины реза: количество строки резки — это и есть пог.м.
        # Не-резка (гравировка, монтаж) длины реза не имеет — расхода нет.
        if item.service_id and item.service.uses_running_meter:
            return recipe.consumption_per_unit * item.quantity
        return Decimal("0")
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


def _deduct_stock_for_item(item: TransactionItem, user, *, restore=False, happened_at=None) -> list:
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
    warnings = _move_stock_for_item(item, user, restore=restore, happened_at=happened_at)
    InventoryLog.objects.filter(receipt=receipt, id__gt=last_log).update(receipt_item=item)
    if warnings:
        from audit.models import AuditLog

        number = receipt.order_number or receipt.pk
        for warning in warnings:
            AuditLog.record(user, f"Чек {number}: {warning['message']}")
    return warnings


def _move_stock_for_item(item: TransactionItem, user, *, restore=False, happened_at=None) -> list:
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
    # `happened_at` — когда списание произошло на самом деле (отмена возврата:
    # сегодня, парой к сегодняшнему возврату на склад), а не дата заказа.
    extra = {} if restore else {"happened_at": happened_at or receipt.created_at}
    if item.type == TransactionItem.Type.MATERIAL and item.material_id:
        # РУЛОН идёт своим путём — погонными метрами по рулонам.
        #
        # Перевести метры в площадь одним умножением нельзя: у каждого рулона
        # своя ширина, замороженная при приёмке, и 1.4 м оракала шириной 1.0 —
        # это другая площадь и другая себестоимость, чем 1.4 м шириной 1.52.
        # `consume_metres` идёт по рулонам FIFO и у каждого переводит метры ЕГО
        # шириной; со склада уходит вся ширина полотна (режут поперёк целиком,
        # узкая полоса остаётся обрезком цеха).
        # Рулон по площади изделия (CALC-10) режется так же: со склада уходит
        # длина изделия на всю ширину рулона, площадь изделия — только цена.
        if item.sale_mode == TransactionItem.SaleMode.METER or item.roll_area:
            metres = item.length if item.roll_area else item.quantity
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
            if (
                not restore and item.roll_area and item.roll_id
                and item.roll.width and item.width > item.roll.width
            ):
                raise InsufficientStock(
                    f"«{item.material.name}»: изделие {item.width} м шире рулона "
                    f"{item.roll.width} м — так не отрезать. Выберите рулон шире или "
                    "продайте полосы метрами."
                )
            metre_fn = restore_metres if restore else consume_metres
            trace = [] if not restore else None
            lot_args = {"lots": _restore_lots(item)} if restore else {"trace": trace}
            cost = metre_fn(
                item.material, metres, user=user,
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


def _pricing_settings(receipt: Receipt):
    """Настройки цен, один раз на сборку чека (а не на каждую строку)."""
    cached = getattr(receipt, "_pricing_settings", None)
    if cached is None:
        from services.models import PricingSettings

        cached = PricingSettings.load()
        receipt._pricing_settings = cached
    return cached


def _line_rules(receipt: Receipt, item_type, service, part_material=Decimal("0")) -> LineRules:
    """Правила прайса для новой строки этого чека.

    Срочность и скидка — заказа (записаны на чеке при оформлении: дозаказ
    считается по ним же). Минимум — только у строк услуг: своя сумма услуги,
    если задана (0 — без минимума), иначе общая из настроек цен. Материал
    минимумом не облагается: лист, крепёж или кусок под рез продаются по
    своей цене, иначе один саморез стоил бы как работа.

    К чему минимум (`PricingSettings.min_mode`):
    - «работа» — к самой строке работы (как было до 10.10);
    - «деталь» — к работе вместе с материалом ЭТОЙ детали: минимум работы
      уменьшается на стоимость материала (`part_material`), как
      `=МАКС(500; рез + материал)` в Excel; материал дороже минимума — работа
      остаётся по расчёту;
    - «заказ» — минимум строки не действует, заказ поднимает
      `apply_order_minimum` после сборки всех строк.
    """
    settings = _pricing_settings(receipt)
    minimum = Decimal("0")
    if (
        item_type == TransactionItem.Type.SERVICE
        and service is not None
        and settings.min_mode != settings.MinMode.ORDER
    ):
        minimum = service.min_line_amount if service.min_line_amount is not None else settings.min_line_amount
        minimum = minimum or Decimal("0")
        if minimum > 0 and settings.min_mode == settings.MinMode.PART and part_material > 0:
            minimum = max(minimum - part_material, Decimal("0")).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING
            )
    urgency = receipt.urgency_percent if receipt.is_urgent else Decimal("0")
    return LineRules(
        minimum=minimum,
        urgency=urgency or Decimal("0"),
        discount=receipt.discount_percent or Decimal("0"),
    )


def _create_line(receipt: Receipt, *, part_material=Decimal("0"), **fields) -> TransactionItem:
    """Создать строку чека, применив правила прайса к цене за единицу.

    `price_per_item` в `fields` — цена ДО правил (каталожная или вписанная);
    она запоминается в `catalog_price`, а в `price_per_item` уходит цена после
    правил — та, по которой строка стоит в чеке и в отчётах.

    ГАРАНТИЯ: у гарантийного заказа все строки по 0 — материал спишется как
    обычно, а выручки нет (себестоимость переделки видна отдельно).
    """
    rules = _line_rules(receipt, fields.get("type"), fields.get("service"), part_material)
    if fields.get("client_price"):
        # Договорная цена уже учитывает договорённость с клиентом: его скидка
        # к ней не применяется, минимум и срочность заказа — как у всех строк.
        rules = LineRules(minimum=rules.minimum, urgency=rules.urgency, discount=Decimal("0"))
    base = Decimal(fields.pop("price_per_item"))
    if receipt.is_warranty:
        base = Decimal("0")
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


def _line_exact(item: TransactionItem) -> Decimal:
    """Точная (без округления до сома) стоимость строки по записанным на ней
    правилам. Строка, проданная до правил, — её собственная стоимость."""
    if item.catalog_price is None:
        return item.sold_total
    rules = LineRules(
        minimum=item.min_amount or Decimal("0"),
        urgency=item.urgency_percent or Decimal("0"),
        discount=item.discount_percent or Decimal("0"),
    )
    amount, _applied = exact_total(item.quantity, item.catalog_price, rules)
    return amount


def _part_amount(receipt: Receipt, amount: Decimal) -> Decimal:
    """Стоимость части заказа ДО срочности и скидки в том виде, в каком она
    войдёт в сумму: в режиме «по строкам» — вверх до сома, как строка; в режиме
    «итог одной формулой» — точная."""
    if _pricing_settings(receipt).rounding_mode == "LINE":
        return amount.quantize(Decimal("1"), rounding=ROUND_CEILING)
    return amount


def apply_order_minimum(receipt: Receipt, items) -> None:
    """Режим минимума «заказ»: сумма заказа меньше минимума — последняя
    строка работы дорожает на разницу (до срочности и скидки, как и
    минимум строки). Заказ без услуг минимумом не облагается — как и материал
    сам по себе (D-63). Вызывается один раз после сборки строк заказа.
    """
    settings = _pricing_settings(receipt)
    if settings.min_mode != settings.MinMode.ORDER or settings.min_line_amount <= 0:
        return
    items = [i for i in items if not i.is_returned and i.catalog_price is not None]
    # В режиме «по строкам» каждая строка до правил уже округляется вверх до
    # сома, и сумма заказа считается из таких строк — иначе «поднять до 500»
    # дало бы 501–502 на копейках остальных строк.
    amounts = [_part_amount(receipt, i.quantity * i.catalog_price) for i in items]
    total = sum(amounts, Decimal("0"))
    if total <= 0 or total >= settings.min_line_amount:
        return
    for item, amount in reversed(list(zip(items, amounts))):
        if item.type == TransactionItem.Type.SERVICE and item.catalog_price > 0:
            item.min_amount = (amount + (settings.min_line_amount - total)).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING
            )
            reprice_line(item)
            item.save(update_fields=["price_per_item", "min_amount", "min_applied"])
            return


def apply_order_rounding(receipt: Receipt, items) -> None:
    """Режим округления «итог заказа»: вверх до сома округляется СУММА точных
    стоимостей строк, а разница раскладывается по строкам (`allocate_order_total`)
    так, чтобы итог чека оставался суммой строк. По строкам прежний режим."""
    settings = _pricing_settings(receipt)
    if settings.rounding_mode != settings.Rounding.ORDER:
        return
    items = [i for i in items if not i.is_returned and i.catalog_price is not None]
    if len(items) < 2:
        return
    shares = allocate_order_total([_line_exact(i) for i in items])
    for item, share in zip(items, shares):
        if share > 0 and item.quantity > 0:
            price = price_for_target(item.quantity, share)
            if price != item.price_per_item:
                item.price_per_item = price
                item.save(update_fields=["price_per_item"])


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


class OrderRejected(Exception):
    """Заказ нельзя оформить из-за превышения жёсткого предела (400)."""


class NeedsConfirmation(Exception):
    """Заказ сомнителен — касса должна переспросить человека (409).

    Пока клиент не пришлёт `confirmed_warnings` с кодами этих предупреждений,
    транзакция откатывается и ничего не создаётся: «сантиметры вместо метров»
    не должны превращаться в чек на миллиард одним нажатием.
    """

    def __init__(self, warnings):
        super().__init__("Нужно подтверждение.")
        self.warnings = warnings


# Предупреждения, которые обязаны быть подтверждены явно.
CONFIRM_CODES = ("line_total_high", "size_exceeds_sheet", "debt_over_limit")


def _fits_sheet(width, length, sheet_w, sheet_h) -> bool:
    """Деталь помещается в лист (с поворотом на 90°)."""
    return (width <= sheet_w and length <= sheet_h) or (width <= sheet_h and length <= sheet_w)


def order_warnings(receipt: Receipt, items, *, client=None, with_debt=True) -> list:
    """Предупреждения «спросить человека» для строк, собранных в этом запросе.

    - `line_total_high` — сумма строки выше порога настройки (по умолчанию
      100 000);
    - `size_exceeds_sheet` — деталь больше листа материала в любую сторону:
      сантиметры вместо метров или опечатка в размере;
    - `debt_over_limit` — долг клиента после заказа выше его лимита или у
      клиента есть долг старше N дней (настройка), а этот заказ добавляет долг.
    """
    settings = _pricing_settings(receipt)
    warnings = []
    threshold = settings.confirm_line_total
    for item in items:
        if item.is_returned:
            continue
        name = _line_name(item)
        total = item.sold_total
        if threshold > 0 and total > threshold:
            warnings.append({
                "code": "line_total_high", "item": item.id, "name": name,
                "line_total": total, "threshold": threshold,
                "message": (
                    f"«{name}»: сумма строки {total} сом выше порога {threshold} сом — "
                    "проверьте размеры и количество."
                ),
            })
        mat = item.work_material if item.type == TransactionItem.Type.SERVICE else None
        if mat is not None and item.width and item.length and mat.sheet_width and mat.sheet_height:
            if not _fits_sheet(item.width, item.length, mat.sheet_width, mat.sheet_height):
                warnings.append({
                    "code": "size_exceeds_sheet", "item": item.id, "name": name,
                    "message": (
                        f"«{name}»: деталь {item.width}×{item.length} м больше листа "
                        f"«{mat.name}» ({mat.sheet_width}×{mat.sheet_height} м) — "
                        "проверьте размеры (метры, не сантиметры)."
                    ),
                })
    if with_debt:
        warnings += debt_warnings(receipt, client)
    return warnings


def debt_warnings(receipt: Receipt, client=None) -> list:
    """Предупреждение о долге клиента при заказе в долг (CLI-03).

    Лимит — `Client.effective_credit_limit` (свой или общий; пусто или поля нет
    — лимита нет). Возраст — самый старый долг клиента с признанной
    выручкой старше `debt_warn_days` дней (0 — не проверять). Предупреждаем
    только когда ЭТОТ заказ добавляет долг: заказ, оплаченный целиком, риска не
    несёт.
    """
    client = client or receipt.client
    if client is None:
        return []
    new_debt = receipt.debt
    if new_debt <= 0:
        return []
    from clients.opening import opening_debt, opening_oldest

    others = [r for r in client.receipts.exclude(pk=receipt.pk) if r.debt > 0]
    # Входящий долг на дату переезда (волна 2) — тоже долг клиента.
    before = sum((r.debt for r in others), Decimal("0")) + opening_debt(client)
    after = before + new_debt
    out = []
    # Лимит клиента — свой или общий из настроек клиентов (`effective_credit_limit`);
    # карточка без этого свойства — просто `credit_limit`; пусто — лимита нет.
    limit = getattr(client, "effective_credit_limit", None)
    if limit is None:
        limit = getattr(client, "credit_limit", None)
    if limit is not None and after > limit:
        out.append({
            "code": "debt_over_limit", "reason": "limit", "limit": limit,
            "debt_before": before, "debt_after": after,
            "message": (
                f"Долг клиента после заказа {after} сом выше его лимита {limit} сом."
            ),
        })
    days = _pricing_settings(receipt).debt_warn_days
    opening_at = opening_oldest(client)
    if days and (others or opening_at is not None):
        today = timezone.localdate()
        moments = [r.created_at for r in others] + ([opening_at] if opening_at is not None else [])
        oldest = min(timezone.localtime(m).date() for m in moments)
        age = (today - oldest).days
        if age > days:
            out.append({
                "code": "debt_over_limit", "reason": "age", "days": age,
                "debt_before": before, "debt_after": after,
                "message": (
                    f"У клиента уже есть долг {before} сом, ему {age} дн. (порог {days} дн.). "
                    "Отгружать ещё в долг?"
                ),
            })
    return out


def check_order_limits(
    receipt: Receipt, items, user, confirmed=(), *, with_debt=True, raise_pending=True,
) -> list:
    """Проверки оформления после сборки строк (внутри транзакции продажи).

    Жёсткий потолок складовщика — `OrderRejected` (400). Остальные сомнения
    без подтверждения — `NeedsConfirmation` (409), и вызывающий откатывает
    заказ. Возвращает предупреждения, которые подтверждены (их показывают
    после оформления).
    """
    settings = _pricing_settings(receipt)
    cap = settings.staff_line_cap
    if cap > 0 and not getattr(user, "is_admin_role", False):
        for item in items:
            if not item.is_returned and item.sold_total > cap:
                raise OrderRejected(
                    f"«{_line_name(item)}»: сумма строки {item.sold_total} сом выше "
                    f"потолка {cap} сом для складовщика. Позовите администратора."
                )
    found = order_warnings(receipt, items, with_debt=with_debt)
    confirmed = set(confirmed or ())
    pending = [
        w for w in found
        if w["code"] in CONFIRM_CODES and w["code"] not in confirmed and "*" not in confirmed
    ]
    if pending and raise_pending:
        raise NeedsConfirmation(pending)
    return found


def _contract_prices(receipt: Receipt) -> dict:
    """Договорные цены клиента заказа (CLI-02, волна 2): {(услуга, материал,
    единица): цена}. Читаются один раз на сборку чека. У гарантийного заказа и
    заказа без клиента их нет."""
    cached = receipt.__dict__.get("_contract_prices")
    if cached is None:
        cached = {}
        if receipt.client_id and not receipt.is_warranty:
            from clients.models import ClientPrice

            for cp in ClientPrice.objects.filter(client_id=receipt.client_id):
                cached[(cp.service_id, cp.material_id, cp.sale_mode or "")] = cp.price
        receipt.__dict__["_contract_prices"] = cached
    return cached


def _contract_for_material(receipt: Receipt, material, mode):
    return _contract_prices(receipt).get((None, material.pk, mode or ""))


def _contract_for_service(receipt: Receipt, service, material=None):
    prices = _contract_prices(receipt)
    if material is not None:
        hit = prices.get((service.pk, material.pk, ""))
        if hit is not None:
            return hit
    return prices.get((service.pk, None, ""))


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
        roll_area = {}
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
        elif material.sells_roll_by_area and entry.get("width") and entry.get("length"):
            # РУЛОН ПО ПЛОЩАДИ ИЗДЕЛИЯ (CALC-10, D-140): баннер 1×2 м по 220 сом
            # за кв.м = 440. Количество — площадь изделия (тем же округлением,
            # что у куска листа), размеры — на строке: по длине со склада
            # уходит вся ширина рулона (`_move_stock_for_item`), по ширине
            # считается обрезок. Цена — именно `price_per_sqm` рулона, без
            # запасной `price_per_unit`.
            mode = TransactionItem.SaleMode.SQM
            roll_area = {
                "roll_area": True,
                "width": Decimal(str(entry["width"])),
                "length": Decimal(str(entry["length"])),
            }
            qty = _area(roll_area["width"], roll_area["length"])
            price = _priced("material_price", material.price_per_sqm)
        else:
            mode = TransactionItem.SaleMode.SQM
            price = _priced(
                "material_price",
                material.sqm_price if material.is_roll_material else material.price_per_unit,
            )
        # Договорная цена клиента (волна 2) — вместо каталога, если цену не
        # вписали руками.
        contract = None
        if _override("material_price") is None:
            contract = _contract_for_material(receipt, material, mode)
            if contract is not None:
                price = contract
        return [_create_line(
            receipt, type=item_type, material=material,
            quantity=qty, price_per_item=price,
            price_is_manual=_override("material_price") is not None,
            client_price=contract is not None,
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
            **roll_area,
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
            price_is_manual=_override("cut_rate") is not None,
            sale_mode=mode,
            # Размеры — только у площадной мерки: у метров и штук их нет, и
            # чужие цифры в этих колонках врали бы о том, что мерили.
            width=Decimal(str(width)) if mode == TransactionItem.SaleMode.SQM and width else None,
            length=Decimal(str(length)) if mode == TransactionItem.SaleMode.SQM and length else None,
            note=(entry.get("note") or "")[:255],
            executor=entry.get("executor"),
        )]

    # Area-priced services: cutting, engraving and interior install. Work is
    # computed automatically from the cut area (width × length × число деталей).
    if service.uses_area:
        width = entry.get("width")
        length = entry.get("length")
        parts = _parts(entry)
        passes = _passes(entry)
        # «Деталей, шт»: площадь и длина реза считаются на ВСЕ детали и
        # округляются один раз — 12 × 0.33×0.37 это 12 × 0.1221 = 1.465, а не
        # 12 × 0.122 = 1.464.
        if width and length:
            area = _qty(Decimal(str(width)) * Decimal(str(length)) * parts)
        else:
            area = _qty(entry.get("quantity") or 0)
        # Материал КЛИЕНТА: со склада ничего не уходит, строки материала нет.
        # Сериализатор материал при этом флаге не пропускает; здесь — на случай
        # прямого вызова, чтобы «чужой» лист точно не списался.
        own_material = bool(entry.get("own_material"))
        material = None if own_material else entry.get("material")

        # Ставка работы — `services.pricing.resolve_rate`: матрица (материал →
        # толщина) → ставка станка → ставка материала, к последним двум
        # коэффициент по толщине. Ручная ставка заменяет всю цепочку, но
        # «проходы» умножают и её. Любую ставку админ может перебить в момент
        # продажи (складовщик — там, где это разрешено услуге).
        manual_rate = _override("cut_rate")
        coef = None
        work_contract = None
        if manual_rate is not None:
            rate = manual_rate
        else:
            # Договорная ставка клиента (волна 2): «услуга + материал», потом
            # «услуга»; коэффициент толщины к ней не применяется (как к матрице).
            work_contract = _contract_for_service(receipt, service, material)
            if work_contract is not None:
                rate = work_contract
            else:
                resolved = resolve_rate(service, material)
                rate, coef = resolved.rate, resolved.coefficient
        rate = apply_passes(rate, passes)
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
            work_qty = _qty(Decimal(str(rm)) * parts) if rm not in (None, "") else Decimal("0")
        # Материал куска — отдельной строкой (площадь × цена за кв.м или
        # вписанная цена). Рез целого листа размеров не имеет (площадь 0) —
        # лист продаётся своей строкой PIECE, здесь только работа.
        sell_material = bool(service.uses_material and material and area > 0)
        material_price = (
            _priced("material_price", material.sqm_price) if sell_material else Decimal("0")
        )
        material_contract = None
        if sell_material and _override("material_price") is None:
            material_contract = _contract_for_material(receipt, material, TransactionItem.SaleMode.SQM)
            if material_contract is not None:
                material_price = material_contract
        work = _create_line(
            receipt, type=TransactionItem.Type.SERVICE, service=service,
            quantity=work_qty, price_per_item=rate,
            price_is_manual=manual_rate is not None,
            client_price=work_contract is not None,
            # Режим «деталь»: минимум работы уменьшается на материал этой же детали.
            part_material=(
                _part_amount(receipt, area * material_price) if sell_material else Decimal("0")
            ),
            width=Decimal(str(width)) if width else None,
            length=Decimal(str(length)) if length else None,
            parts_count=parts, passes=passes, thickness_coef=coef,
            work_material=material,
            own_material=own_material,
            note=(entry.get("note") or "")[:255],
            # Исполнитель — у строки РАБОТЫ; у строки материала его нет.
            executor=entry.get("executor"),
        )
        items = [work]
        if sell_material:
            items.append(_create_line(
                receipt, type=TransactionItem.Type.MATERIAL, material=material,
                quantity=area, price_per_item=material_price,
                price_is_manual=_override("material_price") is not None,
                client_price=material_contract is not None,
                sale_mode=TransactionItem.SaleMode.SQM,
                parts_count=parts,
                # Режут из ВЫБРАННОЙ пачки — как и при обычной продаже листа.
                roll=entry.get("roll") if material.is_roll_material else None,
            ))
        return items

    # Услуга за штуку или фикс (наружная установка, монтаж, «Прочее»). Цена —
    # каталожная; «по договорённости» (`negotiable_price`) её вписывают в кассе:
    # поле `cut_rate` — цена за единицу (админ — любая, складовщик — если у
    # услуги стоит флаг, это проверяет вьюха).
    catalogue = service.rate_per_piece if service.uses_pieces else service.base_price
    manual = _override("cut_rate")
    contract = _contract_for_service(receipt, service) if manual is None else None
    if contract is not None:
        catalogue = contract
    return [_create_line(
        receipt, type=item_type, service=service,
        quantity=Decimal(entry.get("quantity") or 1),
        price_per_item=manual if manual is not None else catalogue,
        price_is_manual=manual is not None,
        client_price=contract is not None,
        note=(entry.get("note") or "")[:255],
        executor=entry.get("executor"),
    )]


def _parts(entry) -> int:
    """Число деталей позиции (≥ 1)."""
    try:
        return max(int(entry.get("parts_count") or 1), 1)
    except (TypeError, ValueError):
        return 1


def _passes(entry) -> int:
    """Число проходов позиции (≥ 1)."""
    try:
        return max(int(entry.get("passes") or 1), 1)
    except (TypeError, ValueError):
        return 1


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


def _settle_old_debts(receipt, client, cashier, payment_method, debt_ids, *, surplus, pay_full,
                      pay_opening=False):
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
    if client is None:
        return surplus
    # Входящий долг на дату переезда (волна 2) — ПЕРВЫМ, как в общей выплате:
    # касса называла «к получению» вместе с ним, и без этого шага его деньги
    # осели бы сдачей на новом чеке.
    if pay_opening:
        from clients.opening import OpeningRejected, opening_debt, pay_opening_debts

        take = None if pay_full else min(max(surplus, Decimal("0")), opening_debt(client))
        if take is None or take > 0:
            try:
                alloc, _left = pay_opening_debts(
                    client, take, user=cashier, method=payment_method,
                    note=f"С заказом №{receipt.order_number}",
                )
            except OpeningRejected as e:
                receipt.debt_error = str(e)
                alloc = []
            paid_opening = sum((a for _, a in alloc), Decimal("0"))
            receipt.debt_paid += paid_opening
            if not pay_full:
                surplus -= paid_opening
    if not debt_ids:
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
    receipt.debt_paid += sum((paid for _, paid in allocations), Decimal("0"))
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
    is_warranty=False, warranty_of=None, warranty_reason="", warranty_culprit="",
    buyer_name="", use_advance=False, pay_opening=False,
) -> Receipt:
    """Create a receipt with its line items.

    ``use_advance=True`` — закрыть остаток заказа АВАНСОМ клиента (волна 2,
    D-93): сразу после зачёта сдачи, тем же способом — деньги лежат в кассе с
    дня внесения аванса, поэтому касса не двигается; у чека растут
    `amount_paid` и `change_applied`, аванс уменьшается
    (`clients.advances.take_advance`, запись `BalanceOffset`).

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
        is_warranty=bool(is_warranty),
        warranty_of=warranty_of if is_warranty else None,
        warranty_reason=(warranty_reason or "").strip()[:255] if is_warranty else "",
        warranty_culprit=(warranty_culprit or "").strip()[:120] if is_warranty else "",
        buyer_name=(buyer_name or "").strip()[:255],
        **({"created_at": created_at} if created_at else {}),
    )

    built = []
    for entry in items_data:
        built += _build_item(receipt, entry)  # creates one or more line items
    # Минимум «заказ» и округление «итог одной формулой» видят заказ целиком.
    apply_order_minimum(receipt, built)
    apply_order_rounding(receipt, built)

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
        # Аванс клиента — после сдачи и тоже только на остаток (D-93).
        if use_advance and client is not None and not receipt.is_warranty:
            from clients.advances import advance_available

            offset += min(advance_available(client), total - offset)
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
        if (pay_debt_ids or pay_opening) and (use_change or use_advance) and offset > 0 and not pay_full:
            paid = min(brought, total - offset)
        else:
            paid = min(brought, total)
        receipt.cost_warnings = _deduct_all(receipt)
        if not receipt.is_warranty:
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
            surplus=surplus, pay_full=pay_full, pay_opening=pay_opening,
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

    # Аванс клиента (волна 2) — ПОСЛЕ сдачи и тоже только на остаток. Деньги
    # лежат в кассе с дня внесения аванса — в кассовую книгу зачёт не пишется
    # (поэтому он в `change_applied`, как и сдача: `brought` ниже его не видит).
    receipt.advance_applied = Decimal("0")
    if (
        use_advance and client is not None and not receipt.is_warranty
        and payment_method != Receipt.PaymentMethod.ONLINE
    ):
        owed = total - receipt.amount_paid
        if owed > 0:
            from clients.advances import take_advance

            taken = take_advance(
                client, owed, receipt=receipt, user=cashier,
                used_on=timezone.localtime(receipt.created_at).date(),
            )
            if taken > 0:
                receipt.amount_paid += taken
                receipt.change_applied += taken
                receipt.advance_applied = taken
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


def price_changed_warnings(receipt: Receipt, item: TransactionItem) -> list:
    """Дозаказ дороже или дешевле, чем та же позиция в заказе (G4-N3).

    Прайс поменяли, пока заказ открыт: дозаказ того же листа или той же работы
    уйдёт по НОВОЙ цене, а прежние строки остались по старой. Это не ошибка, но
    кассир должен об этом знать: «было X, стало Y» (цена за единицу до правил).
    Сравниваем с самой свежей прежней строкой того же материала/услуги/способа.
    """
    if item.catalog_price is None:
        return []
    previous = (
        receipt.items.filter(
            type=item.type, material_id=item.material_id, service_id=item.service_id,
            sale_mode=item.sale_mode, catalog_price__isnull=False,
        )
        .exclude(pk=item.pk)
        .order_by("-id")
        .first()
    )
    if previous is None or previous.catalog_price == item.catalog_price:
        return []
    name = _line_name(item)
    return [{
        "code": "price_changed", "item": item.id, "name": name,
        "was": previous.catalog_price, "now": item.catalog_price,
        "message": (
            f"«{name}»: цена изменилась — в заказе было {previous.catalog_price}, "
            f"стало {item.catalog_price} сом."
        ),
    }]


@transaction.atomic
def add_items_to_receipt(receipt: Receipt, items_data, *, user=None, confirmed=()):
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
    receipt.order_warnings = []
    built = []
    for entry in items_data:
        built += _build_item(receipt, entry)
    # Округление «итог одной формулой» — по добавленным строкам: уже принятые
    # строки и их цены не трогаем.
    apply_order_rounding(receipt, built)
    receipt.order_warnings = check_order_limits(receipt, built, user, confirmed, with_debt=False)
    for item in built:
        surcharge += item.line_total
        receipt.cost_warnings += price_changed_warnings(receipt, item)
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


# Способы, которые принимает только явный путь: «списание долга» не деньги и в
# кассу не идёт, поэтому оно не может прийти как способ оплаты чека или сдачи.
SPECIAL_METHODS = (Payment.Method.WRITE_OFF.value,)


def normalize_method(raw, *, allow_special=False):
    """'cash' → 'CASH'; пусто → None («как у чека»); незнакомое → отказ.

    Раньше способ шёл в запись как есть: «cash» строчными не совпадал ни с
    одним значением и молча уходил в банк (`cash.account_for` проверяет только
    точное «CASH»), а в истории оплат оставалась запись с несуществующим
    способом.

    `allow_special` — для приёма оплаты долга: там можно ещё и списать долг
    (`WRITE_OFF`).
    """
    if raw in (None, ""):
        return None
    value = str(raw).strip().upper()
    allowed = list(Receipt.PaymentMethod.values) + (list(SPECIAL_METHODS) if allow_special else [])
    if value not in allowed:
        raise PaymentRejected(f"Неизвестный способ оплаты. Ожидается один из: {', '.join(allowed)}.")
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


def bad_debt_kind():
    """Вид расхода «Безнадёжные долги». Его заводит миграция финансов; если её
    ещё нет (или вид удалили) — создаём здесь, чтобы списание не упало."""
    from finance.models import ExpenseKind

    kind = ExpenseKind.objects.filter(code="BAD_DEBT").first()
    if kind is None:
        kind, _created = ExpenseKind.objects.get_or_create(
            code="BAD_DEBT",
            defaults={
                "name": "Безнадёжные долги", "block": ExpenseKind.Block.VARIABLE,
                "role": ExpenseKind.Role.OPEX, "is_builtin": True, "position": 90,
            },
        )
    return kind


def _record_bad_debt(receipt: Receipt, amount: Decimal, *, day, note, user):
    """Списание долга — расход ОПиУ «Безнадёжные долги». В кассовую книгу он НЕ
    пишется: деньги не двигались, клиент просто не заплатил."""
    from finance.models import ExpenseEntry

    return ExpenseEntry.objects.create(
        kind=bad_debt_kind(),
        name=f"Списание долга по заказу №{receipt.order_number or receipt.pk}",
        amount=amount, spent_at=day, note=note or "", created_by=user,
    )


@transaction.atomic
def apply_payment(
    receipt: Receipt, amount=None, *, user=None, paid_on=None, method=None, note="",
    keep_change=False, use_change=False,
) -> Decimal:
    """Принять оплату долга по чеку. Возвращает РЕАЛЬНО зачтённую в долг сумму.

    `amount=None` — закрыть весь остаток. Больше остатка в долг не зачитываем:
    долг не может уйти в минус.

    `keep_change=True` — лишнее не выбрасывать, а записать СДАЧЕЙ (`change_due`):
    деньги принесли, а вернуть их на руки не смогли. Вызывающий говорит об этом
    явно, потому что общая выплата сама решает, куда девать остаток.

    `use_change=True` (cash-08) — сначала закрыть долг СДАЧЕЙ клиента с его
    других заказов: деньги за неё уже лежат в кассе, поэтому в книгу этот кусок
    не пишется, а в историю оплат идёт отдельной записью «Зачёт сдачи».
    `amount` при этом — сколько принесли НАЛИЧНЫМИ (пусто — весь остаток после
    зачёта); сдача берётся только на то, что деньги не покрыли.

    `method="WRITE_OFF"` (cash-09) — списание безнадёжного долга: долг
    уменьшается, денег в кассе нет, в ОПиУ появляется расход «Безнадёжные
    долги». Только администратор.

    Каждая оплата пишется записью ``Payment`` — с датой, которую можно поставить
    задним числом, и способом оплаты. Оплата наличными по ОНЛАЙН-заказу без
    явного способа идёт в НАЛИЧНЫЕ: человек принёс деньги в кассу, а не заплатил
    шлюзу.
    """
    # Замок и свежее состояние ДО проверок: два одновременных запроса на один
    # долг оба видели «долг есть» и оба проводили оплату (8 × `/pay/` по 100 —
    # восемь Payment и +800 в кассе при `amount_paid` 100).
    lock_receipt(receipt)
    method = normalize_method(method, allow_special=True)
    writing_off = method == Payment.Method.WRITE_OFF
    if writing_off and not getattr(user, "is_admin_role", False):
        raise PaymentRejected("Списать долг может только администратор.")
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
    if writing_off and receipt.revenue_recognized_at is None:
        raise PaymentRejected(
            "Неоплаченный онлайн-счёт не числится долгом — списывать нечего. "
            "Удалите заказ, если он не нужен."
        )
    if method is None and receipt.payment_method == Receipt.PaymentMethod.ONLINE:
        method = Receipt.PaymentMethod.CASH

    change_part = Decimal("0")
    over = Decimal("0")
    if writing_off:
        cash_part = owed if amount is None else min(amount, owed)
    else:
        if use_change and receipt.client_id:
            available = client_change_available(receipt.client, exclude=receipt)
            need = owed if amount is None else max(owed - amount, Decimal("0"))
            take = min(available, need)
            if take > 0:
                change_part = _take_client_change(receipt.client, take, exclude=receipt)
        if amount is None:
            cash_part = owed - change_part
        else:
            cash_part = min(amount, owed - change_part)
            over = amount - cash_part if keep_change else Decimal("0")
    credited = change_part + cash_part
    receipt.amount_paid = receipt.amount_paid + credited
    receipt.change_applied = receipt.change_applied + change_part
    receipt.change_due = receipt.change_due + over
    if receipt.amount_paid >= receipt.total_price - receipt.refunded_amount:
        receipt.payment_status = Receipt.PaymentStatus.PAID
    receipt.save(
        update_fields=["amount_paid", "change_applied", "change_due", "payment_status", "updated_at"]
    )
    # Онлайн-счёт, оплаченный не шлюзом, а в кассе (`/pay/`), — та же продажа,
    # что и подтверждённый шлюзом: признаём и списываем склад. Раньше такой чек
    # становился «Оплачено», а материал со склада не уходил вовсе.
    if receipt.payment_status == Receipt.PaymentStatus.PAID:
        recognize_online_sale(receipt)

    settled_on = paid_on or timezone.localdate()
    if change_part > 0:
        Payment.objects.create(
            receipt=receipt, amount=change_part, method=Payment.Method.CHANGE,
            paid_on=settled_on, note=(note or "Зачтено из сдачи клиента")[:255], created_by=user,
        )
    if writing_off:
        expense = _record_bad_debt(receipt, cash_part, day=settled_on, note=note, user=user)
        Payment.objects.create(
            receipt=receipt, amount=cash_part, method=Payment.Method.WRITE_OFF,
            paid_on=settled_on, note=(note or "")[:255], created_by=user,
            expense_id=expense.pk,
        )
        return credited
    if cash_part > 0:
        Payment.objects.create(
            receipt=receipt, amount=cash_part, method=method or receipt.payment_method,
            paid_on=settled_on, note=(note or "")[:255], created_by=user,
        )
    # Погашение долга — такой же приход денег, как оплата в кассе, и датируется
    # днём, когда деньги реально принесли. В ящик легло ВСЁ принесённое, вместе
    # со сдачей: раньше приходовалась только часть в зачёт долга, а выдача
    # сдачи потом уводила кассу ниже ящика на её сумму.
    cash.receipt_paid(
        receipt, cash_part + over, user=user, happened_on=settled_on,
        method=method or receipt.payment_method,
    )
    return credited


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
    method = normalize_method(method, allow_special=True)
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
    if method == Payment.Method.WRITE_OFF:
        # Списывают долг, а не принимают деньги: «лишней» суммы, которую надо
        # записать сдачей, не бывает.
        change = Decimal("0")
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


# Поля правки размеров строки (STAFF-08). Ширина/длина/число деталей/проходы
# пересчитывают количество и цену по правилам заказа; `machine` меняет услугу
# резки на услугу того же вида с другим станком.
EDIT_DIM_KEYS = {"width", "length", "parts_count", "passes", "machine"}


def _parse_dim_edit(item: TransactionItem, change) -> dict:
    """Разобрать правку размеров строки или отказать (400), а не промолчать."""
    keys = set(change) & EDIT_DIM_KEYS
    if not keys:
        return {}
    if item.type == TransactionItem.Type.MATERIAL:
        if item.sale_mode != TransactionItem.SaleMode.SQM or keys - {"width", "length", "parts_count"}:
            raise ItemEditRejected(
                f"«{_line_name(item)}»: размеры правятся только у материала по площади "
                "(ширина, длина, деталей)."
            )
        if item.roll_area and "parts_count" in keys:
            # Изделие из рулона — одно на строку: длина и есть то, что отрезали.
            raise ItemEditRejected(
                f"«{_line_name(item)}»: у изделия из рулона правятся ширина и длина."
            )
    elif not (item.service_id and item.service.uses_area):
        raise ItemEditRejected(f"«{_line_name(item)}»: у этой строки размеров нет.")
    out = {}
    for key in ("width", "length"):
        if key in change:
            value = _edit_number(change[key], places=3, digits=8, what="Размер")
            if value <= 0:
                raise ItemEditRejected("Размер должен быть больше нуля.")
            out[key] = value
    for key, top in (("parts_count", 1000), ("passes", 20)):
        if key in change:
            try:
                value = int(str(change[key]).strip())
            except (TypeError, ValueError):
                raise ItemEditRejected(f"{key}: ожидается целое число.")
            if not 1 <= value <= top:
                raise ItemEditRejected(f"{key}: от 1 до {top}.")
            out[key] = value
    if "machine" in change:
        from services.models import PrintingService

        if not item.service.uses_running_meter:
            raise ItemEditRejected("Станок меняется только у резки.")
        machine = str(change["machine"] or "").strip().upper()
        candidates = list(
            PrintingService.objects.filter(
                kind=item.service.kind, machine=machine, is_active=True
            )
        )
        if len(candidates) != 1:
            raise ItemEditRejected(
                f"Станок «{machine}»: нужна ровно одна активная услуга резки с этим станком, "
                f"найдено {len(candidates)}."
            )
        out["service"] = candidates[0]
    return out


def _apply_dim_edit(item: TransactionItem, dims: dict, qty, price):
    """Применить разобранную правку размеров к строке. Возвращает новые
    `(количество, цена до правил)` — те, что пойдут в пересчёт по правилам."""
    if not dims:
        return qty, price
    old_parts = item.parts_count or 1
    new_parts = dims.get("parts_count", old_parts)
    width = dims.get("width", item.width)
    length = dims.get("length", item.length)
    item.width, item.length, item.parts_count = width, length, new_parts
    if item.type == TransactionItem.Type.MATERIAL:
        if qty is None and width and length:
            qty = _qty(width * length * new_parts)
        return qty, price
    # Работа. Цена за единицу: проходы умножают ставку «за один проход»; смена
    # станка берёт ставку нового станка (если цену не вписывали руками).
    old_passes = item.passes or 1
    new_passes = dims.get("passes", old_passes)
    if price is None and item.catalog_price is not None and new_passes != old_passes:
        price = apply_passes(item.catalog_price / old_passes, new_passes)
    item.passes = new_passes
    if "service" in dims:
        item.service = dims["service"]
        if price is None and not item.price_is_manual:
            resolved = resolve_rate(item.service, item.work_material)
            price = apply_passes(resolved.rate, new_passes)
            item.thickness_coef = resolved.coefficient
    if item.service.uses_running_meter:
        if qty is None and new_parts != old_parts:
            qty = _qty(item.quantity / old_parts * new_parts)
    elif qty is None and width and length:
        qty = _qty(width * length * new_parts)
    return qty, price


def _edit_executor(item: TransactionItem, raw):
    """Исполнитель из правки состава: id работающего сотрудника или пусто."""
    from accounts.models import Employee

    if raw in (None, "", 0):
        return None
    if item.type != TransactionItem.Type.SERVICE:
        raise ItemEditRejected("Исполнитель бывает только у работы, не у материала.")
    try:
        employee = Employee.objects.get(pk=int(raw))
    except (Employee.DoesNotExist, TypeError, ValueError):
        raise ItemEditRejected("Сотрудник не найден.")
    if not employee.is_active and employee.pk != item.executor_id:
        raise ItemEditRejected("Сотрудник отключён — выберите работающего.")
    return employee.pk


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
    # Правка одних исполнителей денег не трогает — пересчёт оплаты не нужен.
    touched = False

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
        unknown = set(change) - {"id", "quantity", "price_per_item", "remove", "executor"} - EDIT_DIM_KEYS
        if unknown:
            # Раньше лишние поля («width», «machine») молча игнорировались, и
            # правка «проходила» ничего не изменив.
            raise ItemEditRejected(
                f"Неизвестные поля правки: {', '.join(sorted(unknown))}. Можно: "
                "quantity, price_per_item, remove, width, length, parts_count, passes, machine, "
                "executor."
            )
        if "executor" in change:
            # Исполнитель работы (волна 2): склада и денег не касается — только
            # чья это выработка в ведомости.
            item.executor_id = _edit_executor(item, change["executor"])
            if set(change) <= {"id", "executor"}:
                item.save(update_fields=["executor"])
                continue
        touched = True
        remove = bool(change.get("remove"))
        qty = price = None
        dims = {}
        if not remove:
            dims = _parse_dim_edit(item, change)
        if not remove:
            if change.get("quantity") is not None:
                if item.roll_area:
                    # Площадь изделия из рулона — ширина × длина: правка одной
                    # площади разошлась бы с длиной, по которой списан рулон.
                    raise ItemEditRejected(
                        f"«{_line_name(item)}»: площадь изделия из рулона не правится — "
                        "поправьте ширину или длину."
                    )
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

        qty, price = _apply_dim_edit(item, dims, qty, price)
        if qty is not None:
            item.quantity = qty
        # Цена из правки — цена ДО правил прайса (как в кассе): минимум,
        # срочность и скидка строки пересчитываются от неё по тем правилам,
        # что были при продаже. У строки, проданной до правил, — как раньше.
        reprice_line(item, base_price=price)
        item.save(update_fields=[
            "quantity", "price_per_item", "catalog_price", "min_applied",
            "width", "length", "parts_count", "passes", "service", "thickness_coef",
            "executor",
        ])

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

    if touched:
        _resettle(receipt, held=held)
    return receipt


@transaction.atomic
def give_change(receipt: Receipt, amount=None, *, user=None, method=None) -> Decimal:
    """Выдать клиенту сдачу — целиком или часть. Возвращает выданную сумму.

    Зеркало приёма оплаты: там деньги пришли, тут ушли. Частичная выдача нужна
    потому, что мелочи в кассе может не хватить и во второй раз тоже — «отдал
    тысячу из полутора» это нормальная ситуация цеха, а не ошибка.

    `method` (cash-02) — откуда отдали: наличные (по умолчанию, как всегда)
    или перевод с банка. Сдачу, пришедшую переводом, часто и возвращают
    переводом.
    """
    lock_receipt(receipt)
    method = normalize_method(method)
    due = receipt.change_due
    if due <= 0:
        raise PaymentRejected("По этому заказу сдачи нет.")
    give = due if amount is None else min(amount, due)
    if give <= 0:
        raise PaymentRejected("Сумма выдачи должна быть больше нуля.")
    receipt.change_due = due - give
    receipt.save(update_fields=["change_due", "updated_at"])
    if method is None or method == Receipt.PaymentMethod.CASH:
        cash.change_given(receipt, give, user=user)
    else:
        from finance.models import CashEntry

        cash.money_out(
            give, CashEntry.Article.CHANGE, account=cash.account_for(method),
            receipt=receipt, user=user,
            note=f"Сдача по заказу №{receipt.order_number}" if receipt.order_number else "",
        )
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
    # Часть, зачтённая из АВАНСА клиента (волна 2), возвращается в его авансы, а
    # не сдачей на другой заказ: это деньги «на будущие работы», и у клиента без
    # других заказов они иначе пропали бы вместе с удалённым чеком.
    from clients.advances import release_receipt_advance

    to_advance = min(release_receipt_advance(receipt), applied)
    receipt.change_applied = Decimal("0")
    applied -= to_advance
    if applied <= 0:
        receipt.save(update_fields=["change_applied", "updated_at"])
        return to_advance
    host = None
    if receipt.client_id:
        host = (
            Receipt.objects.filter(client_id=receipt.client_id)
            .exclude(pk=receipt.pk)
            .order_by("-created_at", "-id")
            .first()
        )
    if host:
        host.change_due += applied
        host.save(update_fields=["change_due", "updated_at"])
    else:
        receipt.change_due += applied
    receipt.save(update_fields=["change_applied", "change_due", "updated_at"])
    return applied + to_advance


def writeoff_total(receipt: Receipt) -> Decimal:
    """Сколько долга по чеку списано как безнадёжный (деньгами не пришло)."""
    return sum(
        (p.amount for p in receipt.payments.all() if p.method == Payment.Method.WRITE_OFF),
        Decimal("0"),
    )


def drop_writeoff_expenses(receipt: Receipt) -> None:
    """Убрать расходы «Безнадёжные долги», порождённые списаниями по чеку
    (откат оплаты и отмена списания)."""
    from finance.models import ExpenseEntry

    ids = [
        p.expense_id for p in receipt.payments.all()
        if p.method == Payment.Method.WRITE_OFF and p.expense_id
    ]
    if ids:
        ExpenseEntry.objects.filter(pk__in=ids).delete()


def _refund_cash_out(receipt: Receipt) -> Decimal:
    """Сколько денег по чеку ушло клиенту возвратом (расходы минус приходы статьи
    «Возврат» по этому чеку)."""
    from finance.models import CashEntry

    out = Decimal("0")
    for kind, amount in CashEntry.objects.filter(
        receipt=receipt, article=CashEntry.Article.REFUND
    ).values_list("kind", "amount"):
        out += amount if kind == CashEntry.Kind.OUT else -amount
    return out


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
    if not receipt.client_id:
        return
    held = sum(cash.held_by_account(receipt).values(), Decimal("0"))
    claim = receipt.amount_paid - receipt.change_applied + receipt.change_due
    # Деньги, уже отданные клиенту возвратом, из ящика ушли (cash-10: чек с
    # частичным возвратом проверяли «с закрытыми глазами» и удаляли, оставляя
    # у другого заказа оплату без денег). Поэтому ждём в ящике на столько
    # меньше. Записей возврата нет (старые чеки) — считаем как раньше.
    claim -= _refund_cash_out(receipt)
    # Списанный долг денег в кассу не приносил.
    claim -= writeoff_total(receipt)
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


def _price_for_total(qty: Decimal, total: Decimal) -> Decimal | None:
    """Цена за единицу (2 знака), при которой строка из `qty` стоит ровно
    `total` сом по правилу чека (вверх до сома). None — такой цены нет."""
    if total <= 0:
        return Decimal("0")
    base = total / qty
    for candidate in (
        base.quantize(Decimal("0.01"), rounding=ROUND_FLOOR),
        base.quantize(Decimal("0.01"), rounding=ROUND_CEILING),
    ):
        if (qty * candidate).quantize(Decimal("1"), rounding=ROUND_CEILING) == total:
            return candidate
    return None


def split_line_for_refund(item: TransactionItem, qty) -> TransactionItem:
    """Отделить от строки `qty` единиц в НОВУЮ строку — для частичного возврата
    количества (cash-09, волна 2). Возвращает новую строку (её и возвращают).

    ПРАВИЛО ОКРУГЛЕНИЯ — без потери сома. Строка стоила S = ⌈q × p⌉ (вверх до
    сома). Остающаяся часть считается тем же правилом, что любая строка:
    K = ⌈(q − r) × p⌉; возвращаемая получает остаток R = S − K, а её цена за
    единицу подбирается до тыйына так, чтобы ⌈r × p′⌉ = R. Итог чека K + R = S —
    не меняется ни на сом. Подобрать такую цену нельзя (бывает при количестве
    больше сотни единиц) — отказ: вернуть строку целиком или поправить состав.

    Склад и себестоимость делятся ПРОПОРЦИОНАЛЬНО количеству: каждая партия
    строки (`lot_uses`) отдаёт новой строке долю r/q своей площади и метров,
    себестоимость — та же доля (до тыйына; остаток — на остающейся строке).
    Поэтому возврат кладёт материал в те партии, откуда его взяли, а прибыль дня
    продажи не меняется: продажей там стоят обе строки.

    Только материал и работы без размеров и техкарты: у реза количество — длина
    реза, а расходник техкарты считается от площади или «на строку», и делить
    его по количеству было бы догадкой.
    """
    qty = _qty(qty)
    q = item.quantity
    if item.is_returned:
        raise ItemEditRejected("Строка уже возвращена.")
    if qty <= 0:
        raise ItemEditRejected("Количество возврата должно быть больше нуля.")
    if qty >= q:
        raise ItemEditRejected("Возвращается всё количество строки — верните её целиком.")
    if item.roll_area or (item.type == TransactionItem.Type.SERVICE and (
        item.width or item.length or (item.service_id and item.service.recipes.exists())
    )):
        # Изделие из рулона по кв.м (CALC-10) — тоже с размерами: часть площади
        # без части длины не бывает.
        raise ItemEditRejected(
            "Часть количества возвращается только у материала и работ без размеров и техкарты. "
            "Верните строку целиком или поправьте состав заказа (администратор)."
        )
    total = item.sold_total
    keep = q - qty
    kept_total = (keep * item.price_per_item).quantize(Decimal("1"), rounding=ROUND_CEILING)
    back_total = total - kept_total
    back_price = _price_for_total(qty, back_total)
    if back_price is None:
        raise ItemEditRejected(
            "Эту часть нельзя отделить без сдвига суммы заказа на сом — верните строку "
            "целиком или поправьте количество в «Правке состава»."
        )
    share = qty / q
    back_cost = _money((item.cost_total or Decimal("0")) * share)
    uses = []
    for use in item.lot_uses.order_by("id"):
        area = (use.area * share).quantize(Decimal("0.000001"))
        metres = (use.metres * share).quantize(Decimal("0.000001")) if use.metres is not None else None
        use.area -= area
        if metres is not None:
            use.metres -= metres
        use.save(update_fields=["area", "metres"])
        uses.append((use.roll_id, area, metres))

    part = TransactionItem.objects.get(pk=item.pk)
    part.pk = None
    part.id = None
    part._state.adding = True
    part.quantity = qty
    part.price_per_item = back_price
    if item.catalog_price is not None and item.catalog_price == item.price_per_item:
        part.catalog_price = back_price          # правил не было — и у части их нет
    part.cost_total = back_cost
    part.issued_qty = max(Decimal("0"), item.issued_qty - keep)
    part.save()
    TransactionItemLot.objects.bulk_create(
        TransactionItemLot(item=part, roll_id=pk, area=area, metres=metres) for pk, area, metres in uses
    )
    item.quantity = keep
    item.cost_total = (item.cost_total or Decimal("0")) - back_cost
    item.issued_qty = min(item.issued_qty, keep)
    item.save(update_fields=["quantity", "cost_total", "issued_qty"])
    return part


@transaction.atomic
def refund_receipt(receipt: Receipt, *, item_ids=None, user=None, method=None, quantities=None) -> Receipt:
    """Refund the whole receipt or specific line items, returning stock.

    Returns deducted materials back to the warehouse and updates statuses.

    `method` (cash-02) — с какого счёта отдали деньги: пусто — с того, куда они
    пришли (как всегда); «CASH» / «MBANK» / «DEMIRBANK» — явно.

    `quantities` (cash-09, волна 2) — `{id строки: сколько вернуть}`: часть
    количества строки отделяется в новую строку (`split_line_for_refund`) и
    возвращается она; всё количество — строка целиком.
    """
    method = normalize_method(method)
    # Замок до чтения строк: 8 параллельных возвратов видели одни и те же
    # невозвращённые строки и писали 8 записей REFUND в кассу.
    lock_receipt(receipt)
    if quantities:
        total_before = receipt.total_price
        item_ids = list(item_ids or [])
        for raw_id, raw_qty in quantities.items():
            try:
                line = receipt.items.select_related("service").get(pk=raw_id, is_returned=False)
            except (TransactionItem.DoesNotExist, ValueError, TypeError):
                raise ItemEditRejected("Строка не найдена в этом чеке или уже возвращена.")
            value = _edit_number(raw_qty, places=3, digits=12, what="Количество возврата")
            if value >= line.quantity:
                item_ids.append(line.pk)
            else:
                item_ids.append(split_line_for_refund(line, value).pk)
        # Итог чека — сумма строк; деление строки его не меняет (правило выше).
        if receipt.recalculate_total() != total_before:
            raise ItemEditRejected("Деление строки сдвинуло сумму заказа — возврат отменён.")
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
    paid_out = _excess() - excess_before
    if method is None:
        cash.refund_paid(receipt, paid_out, user=user)
    else:
        from finance.models import CashEntry

        cash.money_out(
            paid_out, CashEntry.Article.REFUND, account=cash.account_for(method),
            receipt=receipt, user=user,
            note=f"Возврат по заказу №{receipt.order_number}" if receipt.order_number else "",
        )
    return receipt


# --- Отмена ОДНОЙ оплаты, отмена возврата, выдача по позициям, пересчёт ------

@transaction.atomic
def cancel_payment(receipt: Receipt, payment_id, *, user=None, reason="") -> Decimal:
    """Отменить одну принятую оплату долга (cash-03), а не весь заказ.

    Раньше ошибочный платёж можно было только откатить вместе со ВСЕМИ оплатами
    чека — терялась история остальных. Теперь запись оплаты убирается, а в
    кассовую книгу пишется встречный расход (сама исходная запись остаётся:
    книга не подчищается). Зачёт сдачи возвращает сдачу клиенту, списание долга
    убирает свой расход «Безнадёжные долги». След остаётся в журнале действий.
    Первую оплату, принятую при оформлении (записи `Payment` у неё нет),
    откатывает «Откат оплаты» целиком. Возвращает отменённую сумму.
    """
    lock_receipt(receipt)
    if receipt.status == Receipt.Status.CANCELLED or receipt.payment_status in (
        Receipt.PaymentStatus.REFUNDED, Receipt.PaymentStatus.PARTIALLY_REFUNDED,
    ):
        raise PaymentRejected("По возвращённому чеку отмена оплаты недоступна.")
    try:
        payment = receipt.payments.get(pk=payment_id)
    except (Payment.DoesNotExist, ValueError, TypeError):
        raise PaymentRejected("Оплата не найдена в этом заказе.")
    amount = payment.amount
    if payment.method == Payment.Method.CHANGE:
        # Сдача возвращается клиенту — на его другой заказ, как при откате.
        receipt.change_applied = max(receipt.change_applied - amount, Decimal("0"))
        _return_change_to_client(receipt, amount)
    elif payment.method == Payment.Method.WRITE_OFF:
        from finance.models import ExpenseEntry

        if payment.expense_id:
            ExpenseEntry.objects.filter(pk=payment.expense_id).delete()
    else:
        label = f"Отмена оплаты по заказу №{receipt.order_number}" if receipt.order_number else "Отмена оплаты"
        cash.payment_reverted(
            receipt, amount, user=user,
            note=f"{label}: {reason}".strip(": ") if reason else label,
        )
    receipt.amount_paid = max(receipt.amount_paid - amount, Decimal("0"))
    receipt.payment_status = Receipt.PaymentStatus.PENDING
    receipt.save(update_fields=["amount_paid", "change_applied", "change_due", "payment_status", "updated_at"])
    payment.delete()
    return amount


def _return_change_to_client(receipt: Receipt, amount: Decimal) -> None:
    """Вернуть клиенту `amount` сдачи, зачтённой в этот заказ: на самый свежий
    из его других заказов, иначе сдачей на этот же (как `return_applied_change`)."""
    host = None
    if receipt.client_id:
        host = (
            Receipt.objects.filter(client_id=receipt.client_id)
            .exclude(pk=receipt.pk)
            .order_by("-created_at", "-id")
            .first()
        )
    if host:
        host.change_due += amount
        host.save(update_fields=["change_due", "updated_at"])
    else:
        receipt.change_due += amount


@transaction.atomic
def undo_refund(receipt: Receipt, *, item_ids=None, user=None) -> Receipt:
    """Отменить возврат (cash-09): строки снова проданы, материал снова ушёл со
    склада, деньги, отданные клиенту, возвращаются в кассу.

    Возврат оформили по ошибке — раньше исправить это было нечем, кроме нового
    заказа. Отменяются все возвращённые строки чека либо перечисленные. Склад
    списывается заново (не хватает — `InsufficientStock`, ничего не меняется).
    Деньги в кассу приходом по статье «Возврат» на тот счёт, откуда они ушли.
    """
    lock_receipt(receipt)
    lines = receipt.items.filter(is_returned=True)
    if item_ids:
        lines = lines.filter(id__in=item_ids)
    lines = list(lines)
    if not lines:
        raise ItemEditRejected("Отменять нечего: возвращённых позиций нет.")

    def _excess():
        return max(receipt.amount_paid - (receipt.total_price - receipt.refunded_amount), Decimal("0"))

    excess_before = _excess()
    stock_was_deducted = _stock_was_deducted(receipt)
    restored = Decimal("0")
    for item in lines:
        restored += item.sold_total
        item.is_returned = False
        item.returned_at = None
        item.save(update_fields=["is_returned", "returned_at"])
        if stock_was_deducted:
            # Списание — СЕГОДНЯ, парой к возврату на склад (он тоже сегодняшний):
            # месяц заказа получил бы второе списание, а склад по месяцам
            # перестал бы сходиться с проданным.
            _deduct_stock_for_item(item, user, happened_at=timezone.now())
    receipt.refunded_amount = max(receipt.refunded_amount - restored, Decimal("0"))
    if receipt.items.filter(is_returned=True).exists():
        receipt.payment_status = Receipt.PaymentStatus.PARTIALLY_REFUNDED
    else:
        receipt.status = Receipt.Status.COMPLETED
        owed_base = receipt.total_price - receipt.refunded_amount
        receipt.payment_status = (
            Receipt.PaymentStatus.PAID if receipt.amount_paid >= owed_base
            else Receipt.PaymentStatus.PENDING
        )
    receipt.save(update_fields=["refunded_amount", "payment_status", "status", "updated_at"])
    back_in = excess_before - _excess()
    if back_in > 0:
        from finance.models import CashEntry

        last = (
            CashEntry.objects.filter(
                receipt=receipt, article=CashEntry.Article.REFUND, kind=CashEntry.Kind.OUT,
            )
            .order_by("-id")
            .first()
        )
        cash.money_in(
            back_in, CashEntry.Article.REFUND,
            account=last.account if last else cash.account_for(receipt.payment_method),
            receipt=receipt, user=user,
            note=f"Отмена возврата по заказу №{receipt.order_number}" if receipt.order_number else "",
        )
    return receipt


@transaction.atomic
def issue_items(receipt: Receipt, issued, *, user=None) -> Receipt:
    """Выдача по позициям (G1-N4): 8 деталей из 10 вручили в понедельник.

    `issued` — список `{"id": строка, "quantity": сколько выдали СЕЙЧАС}`.
    Статус заказа: все позиции выданы целиком — «Выдан», хоть что-то — «Выдан
    частично». Больше, чем осталось выдать, — отказ.
    """
    lock_receipt(receipt)
    if receipt.status == Receipt.Status.CANCELLED or receipt.payment_status == Receipt.PaymentStatus.REFUNDED:
        raise ItemEditRejected("Заказ отменён и возвращён — выдавать нечего.")
    if not isinstance(issued, list) or not issued:
        raise ItemEditRejected("Не передано ни одной позиции для выдачи.")
    for entry in issued:
        if not isinstance(entry, dict) or set(entry) - {"id", "quantity"}:
            raise ItemEditRejected("Позиция выдачи: ожидаются только поля id и quantity.")
        try:
            item = receipt.items.get(pk=entry.get("id"))
        except (TransactionItem.DoesNotExist, ValueError, TypeError):
            raise ItemEditRejected("Строка не найдена в этом чеке.")
        if item.is_returned:
            raise ItemEditRejected("Строка возвращена клиентом — выдавать её нельзя.")
        qty = _edit_number(entry.get("quantity"), places=3, digits=12, what="Количество")
        if qty <= 0:
            raise ItemEditRejected("Количество к выдаче должно быть больше нуля.")
        if item.issued_qty + qty > item.quantity:
            raise ItemEditRejected(
                f"«{_line_name(item)}»: осталось выдать {item.quantity - item.issued_qty}, "
                f"а указано {qty}."
            )
        item.issued_qty += qty
        item.save(update_fields=["issued_qty"])
    refresh_issue_status(receipt)
    return receipt


def refresh_issue_status(receipt: Receipt) -> None:
    """Статус заказа по выданным позициям: всё выдано / часть / ничего."""
    live = list(receipt.items.filter(is_returned=False))
    if live and all(i.issued_qty >= i.quantity for i in live):
        wanted = Receipt.FulfillmentStatus.ISSUED
    elif any(i.issued_qty > 0 for i in live):
        wanted = Receipt.FulfillmentStatus.PARTIALLY_ISSUED
    else:
        wanted = None
    if wanted and receipt.fulfillment_status != wanted:
        receipt.fulfillment_status = wanted
        receipt.save(update_fields=["fulfillment_status", "updated_at"])


def move_first_payment_cash(receipt: Receipt, old_day, new_day, *, user=None) -> Decimal:
    """Перенос даты заказа двигает деньги его первой оплаты (cash-04).

    Первая оплата — приход, записанный при оформлении, в день заказа. Дату
    заказа перенесли (заказ занесли не тем числом), а деньги остались в старом
    дне: касса и ОДДС того дня врали. Двигаем записи прихода по этому чеку,
    стоявшие на старую дату заказа, — только у обычных заказов (онлайн платит
    в день подтверждения, это факт). Возвращает перенесённую сумму; запись — в
    журнале действий.
    """
    from audit.models import AuditLog
    from finance.models import CashEntry

    if receipt.payment_method == Receipt.PaymentMethod.ONLINE or old_day == new_day:
        return Decimal("0")
    entries = list(
        CashEntry.objects.filter(
            receipt=receipt, article=CashEntry.Article.SALE, kind=CashEntry.Kind.IN,
            happened_on=old_day, is_auto=True,
        ).order_by("id")[:1]
    )
    if not entries:
        return Decimal("0")
    moved = Decimal("0")
    for entry in entries:
        entry.happened_on = new_day
        entry.save(update_fields=["happened_on"])
        moved += entry.amount
    AuditLog.record(
        user,
        f"Перенос даты заказа №{receipt.order_number}: деньги первой оплаты {moved} сом "
        f"перенесены в кассе с {old_day:%d.%m.%Y} на {new_day:%d.%m.%Y}",
    )
    return moved


def _current_base_price(item: TransactionItem):
    """Цена единицы строки ДО правил по сегодняшнему прайсу или None, если
    пересчитать нечем (договорная цена, чужой материал, прайса нет)."""
    if item.type == TransactionItem.Type.MATERIAL:
        material = item.material
        if material is None:
            return None
        if item.sale_mode == TransactionItem.SaleMode.PIECE:
            price = material.piece_price_for_qty(item.quantity)
            if not price and not material.is_roll_material:
                price = material.price_per_unit
        elif item.sale_mode == TransactionItem.SaleMode.METER:
            price = material.price_per_pm
        elif item.roll_area:
            # Рулон по кв.м изделия: только его цена за кв.м (пусто — не трогаем).
            price = material.price_per_sqm
        else:
            price = material.sqm_price if material.is_roll_material else material.price_per_unit
        return Decimal(price) if price else None
    service = item.service
    if service is None or item.own_material or service.uses_free_measure:
        return None
    if service.uses_area:
        rate = apply_passes(resolve_rate(service, item.work_material).rate, item.passes)
    elif service.uses_pieces:
        rate = service.rate_per_piece
    else:
        rate = service.base_price
    return Decimal(rate) if rate else None


@transaction.atomic
def reprice_receipt(receipt: Receipt, *, user=None) -> dict:
    """Пересчитать открытый неоплаченный заказ по сегодняшнему прайсу (G4-N3).

    Excel пересчитал бы заказ сам, когда поменяли ячейку цены; система хранит
    цену строки снимком (и это правильно для оплаченного заказа). Для заказа,
    по которому ещё не брали денег и ничего не выдали, владелец может явно
    переоценить его: строки с ценой, вписанной руками, договорные и работы с
    материалом клиента не трогаются. Минимум, срочность и скидка заказа
    применяются заново от новой цены. Запись — в журнале действий.
    """
    lock_receipt(receipt)
    if (
        receipt.status == Receipt.Status.CANCELLED
        or receipt.payment_status != Receipt.PaymentStatus.PENDING
        or receipt.amount_paid > 0
        or receipt.refunded_amount > 0
        or receipt.change_applied > 0
    ):
        raise ItemEditRejected("Пересчитать можно только открытый заказ, по которому ничего не оплачено.")
    if receipt.fulfillment_status in (
        Receipt.FulfillmentStatus.ISSUED, Receipt.FulfillmentStatus.PARTIALLY_ISSUED,
    ):
        raise ItemEditRejected("Заказ уже выдан клиенту — пересчитывать его нельзя.")
    before = receipt.total_price
    changed, skipped = [], []
    for item in receipt.items.filter(is_returned=False).select_related(
        "material", "service", "work_material"
    ):
        if item.catalog_price is None or item.price_is_manual or item.client_price:
            skipped.append({"item": item.id, "name": _line_name(item)})
            continue
        base = _current_base_price(item)
        if base is None:
            skipped.append({"item": item.id, "name": _line_name(item)})
            continue
        if base == item.catalog_price:
            continue
        was = item.sold_total
        reprice_line(item, base_price=base)
        item.save(update_fields=["price_per_item", "catalog_price", "min_applied"])
        changed.append({
            "item": item.id, "name": _line_name(item),
            "was_total": was, "now_total": item.sold_total, "price": base,
        })
    _resettle(receipt)
    from audit.models import AuditLog

    AuditLog.record(
        user,
        f"Пересчёт заказа №{receipt.order_number or receipt.pk} по текущему прайсу: "
        f"{before} → {receipt.total_price} сом (изменено строк: {len(changed)}, "
        f"не тронуто: {len(skipped)})",
    )
    return {"before": before, "after": receipt.total_price, "changed": changed, "skipped": skipped}


def price_cart(
    *, client, items_data, is_urgent=False, urgency_percent=None, discount_percent=None,
) -> tuple:
    """Собрать корзину в временный чек БЕЗ склада, оплаты и кассы — только цены.

    Для коммерческого предложения (CALC-03): позиции и правила прайса те же,
    что при оформлении, но КП склад, долг и выручку не трогает. Вызывать внутри
    транзакции, которую потом откатывают: чек остаётся только на время расчёта.
    Возвращает `(чек, строки)`.
    """
    receipt = Receipt.objects.create(
        client=client,
        payment_method=Receipt.PaymentMethod.CASH,
        payment_status=Receipt.PaymentStatus.PENDING,
        is_urgent=bool(is_urgent),
        urgency_percent=_order_urgency(urgency_percent) if is_urgent else Decimal("0"),
        discount_percent=discount_percent or Decimal("0"),
    )
    built = []
    for entry in items_data:
        built += _build_item(receipt, entry)
    apply_order_minimum(receipt, built)
    apply_order_rounding(receipt, built)
    receipt.recalculate_total()
    return receipt, built
