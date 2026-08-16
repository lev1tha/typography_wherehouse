"""Core sales business logic: build a receipt, deduct stock, handle payment
confirmation and refunds. Kept separate from the views so it can be reused by
the payment webhook and tested in isolation.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from finance import cash
from warehouse.models import InventoryLog, Material
from warehouse.rolls import consume_area, restore_area
from warehouse.stock import apply_stock_change

from .models import Payment, Receipt, TransactionItem


def _money(value: Decimal) -> Decimal:
    """До копеек. Без этого SQLite сохранил бы «сырой» результат умножения, а
    PostgreSQL округлил бы его сам — и цифры на dev и на проде разошлись бы."""
    return Decimal(value).quantize(Decimal("0.01"))


def _deduct(material, qty, user, reason="", receipt=None, happened_at=None) -> Decimal:
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
        )
    apply_stock_change(
        material, -qty, user=user, reason=reason,
        log_type=InventoryLog.Type.SALE, receipt=receipt, happened_at=happened_at,
    )
    # У штучных материалов партий нет — берём текущую закупочную цену.
    return qty * (material.purchase_price or Decimal("0"))


def _restore(material, qty, user, reason="", receipt=None, happened_at=None) -> Decimal:
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


def _deduct_stock_for_item(item: TransactionItem, user, *, restore=False) -> None:
    """Deduct (or restore) stock for a single line item.

    Cutting now produces two separate lines (a MATERIAL line for the cut material
    and a SERVICE line for the master's work), so the MATERIAL line handles its
    own area; service lines only consume their recipe (technological-card) extras.

    Каждое движение попадает в складской журнал со ссылкой на чек — и продажа
    материала, и расход по техкарте услуги (клей, крепёж).
    """
    from services.models import ServiceRecipe

    fn = _restore if restore else _deduct
    receipt = item.receipt
    # Расход материала датируем заказом (в т.ч. задним числом), а возврат —
    # «сейчас»: возврат случается тогда, когда его оформили, а не когда продали.
    extra = {} if restore else {"happened_at": receipt.created_at}
    if item.type == TransactionItem.Type.MATERIAL and item.material_id:
        # Whole-piece sales deduct the piece area; area/qty sales deduct quantity.
        qty = item.quantity
        if item.sale_mode == TransactionItem.SaleMode.PIECE and item.material.piece_area:
            qty = item.material.piece_area * item.quantity
        cost = fn(
            item.material, qty, user,
            reason=_reason(receipt, restore=restore), receipt=receipt, **extra,
        )
        if not restore:
            item.cost_total = _money(cost)
            item.save(update_fields=["cost_total"])
        return
    if item.type != TransactionItem.Type.SERVICE or not item.service_id:
        return

    # Extra recipe materials (e.g. fasteners for installation, glue, …) — их
    # себестоимость тоже относим на строку услуги.
    cost = Decimal("0")
    reason = _reason(receipt, restore=restore, service=item.service)
    for recipe in item.service.recipes.select_related("material").all():
        if recipe.consumption_mode == ServiceRecipe.Mode.PER_SQM:
            consumed = recipe.consumption_per_unit * item.quantity
        else:  # FIXED per order
            consumed = recipe.consumption_per_unit
        cost += fn(recipe.material, consumed, user, reason=reason, receipt=receipt, **extra)
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
        qty = Decimal(entry.get("quantity") or 0)
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
        # Пока не ввёл, работа = 0: раньше сюда подставлялась площадь и
        # умножалась на ставку за пог.м, то есть кв.м считались как пог.м
        # (для листа 1.22×2.44 — 2.98 вместо реальных 7.32).
        # Для не-резочных площадных услуг (внутренний монтаж) — по-прежнему площадь.
        work_qty = area
        if service.uses_running_meter:
            rm = entry.get("running_meters")
            work_qty = Decimal(str(rm)) if rm not in (None, "") else Decimal("0")
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
def create_sale(
    *, client, cashier, payment_method, items_data, amount_paid=None, title="",
    created_at=None, pay_full=False,
) -> Receipt:
    """Create a receipt with its line items.

    ``pay_full=True`` — «заплатил ровно сколько вышло»: сумма чека известна
    только здесь, после сборки строк, и вызывающий её заранее назвать не может.
    Раньше для этого передавали заведомо большое число (9 999 999), и оно молча
    обрезалось до суммы чека. С тех пор как переплата стала запоминаться сдачей,
    такой «сентинел» превращается в девять миллионов сдачи клиенту. Намерение
    должно быть названо, а не закодировано абсурдным числом.

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
        if pay_full:
            brought = total
        elif amount_paid is None:
            brought = Decimal("0")
        else:
            brought = max(Decimal(str(amount_paid)), Decimal("0"))
        paid = min(brought, total)
        _deduct_all(receipt)
        receipt.stock_deducted = True
        receipt.amount_paid = paid
        receipt.change_due = brought - paid
        receipt.payment_status = (
            Receipt.PaymentStatus.PAID if paid >= total else Receipt.PaymentStatus.PENDING
        )
    else:
        _create_online_invoice(receipt)

    receipt.save()
    # Деньги, принятые при оформлении, — приход в кассовую книгу. Датируем
    # ДАТОЙ ЗАКАЗА: заказ задним числом принёс деньги тогда же, а не сегодня.
    #
    # В кассу кладём то, что клиент ПРИНЁС, а не то, что зачлось за заказ:
    # заказ на 36, принесли 100 — в ящике лежит 100, и 64 из них станут сдачей.
    # Списывается она при выдаче (`give_change`). Записывай мы сюда зачтённые 36,
    # выдача сдачи увела бы кассу в минус на ровном месте.
    brought = receipt.amount_paid + receipt.change_due
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


def _resettle(receipt: Receipt) -> None:
    """Пересчитать итог, статус оплаты и сдачу после правки состава.

    Если итог УПАЛ ниже уже принятых денег — разница не пропадает и не остаётся
    «переплатой»: она становится СДАЧЕЙ, которую цех должен клиенту. Ровно тот
    случай, ради которого правку и просили: написали лишний квадратный метр,
    клиент заплатил по завышенному счёту, потом это нашли.
    """
    total = receipt.recalculate_total()
    owed_base = total - receipt.refunded_amount
    over = receipt.amount_paid - owed_base
    if over > 0:
        receipt.amount_paid = owed_base if owed_base > 0 else Decimal("0")
        receipt.change_due = receipt.change_due + over
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

    _resettle(receipt)
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
    # Возвращаем только НЕвозвращённые строки: по возвращённым материал уже
    # вернулся на склад при возврате, второй раз его класть нельзя.
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
    # Из кассы ушло не больше, чем в неё по этому заказу приходило: вернуть
    # можно только полученные деньги, а неоплаченный заказ возврата денег не
    # порождает вовсе.
    cash.refund_paid(receipt, min(refunded_total, receipt.amount_paid), user=user)
    return receipt
