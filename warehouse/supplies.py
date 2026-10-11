"""Приходная накладная: проведение и отмена.

Документ проводится сразу при создании — строки уходят на склад теми же
примитивами, что и одиночный приход (``receive_lot`` для площадных материалов,
``apply_stock_change`` для штучных). Своей «параллельной» механики склада здесь
нет намеренно: закуп в финотчёте, складской журнал и FIFO считаются по
движениям, и любой второй путь записи рано или поздно с ними разойдётся.
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import InventoryLog, Roll, SupplierReturnLine, Supply, SupplyLine
from .rolls import compute_area, receive_lot
from .stock import apply_stock_change


class SupplyError(Exception):
    """Накладную нельзя провести или отменить — с человеческим объяснением."""


def _moment(day):
    """Дата накладной → момент времени: по нему идут и журнал, и FIFO.

    ПОЛДЕНЬ, а не полночь — та же причина, что у даты заказа: полночь в Бишкеке
    это вчерашний вечер по UTC, и достаточно одной настройки часового пояса
    мимо, чтобы поставка уехала в предыдущий день.
    """
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


def line_quantity(material, form, *, width=None, height=None, length=None,
                  sheet_count=None, quantity=None) -> Decimal:
    """Сколько единиц встанет на склад по строке.

    Площадные материалы считаются в кв.м из размеров, штучные — прямым
    количеством. Одна функция на сервере и на форме: расхождение между тем, что
    показал предпросмотр, и тем, что легло на склад, — худший вид ошибки.
    """
    if form == SupplyLine.Form.QTY or not material.is_roll_material:
        return Decimal(str(quantity or 0))
    # Не хватает размеров — считать нечего. Возвращаем ноль, а объяснение
    # («проверьте размеры») даёт вызывающий: он знает, о какой строке речь.
    need = (width, height, sheet_count) if form == SupplyLine.Form.SHEET else (width, length)
    if not all(need):
        return Decimal("0")
    return compute_area(
        Roll.Form.SHEET if form == SupplyLine.Form.SHEET else Roll.Form.ROLL,
        width=width, length=length, height=height, sheet_count=sheet_count,
    )


@transaction.atomic
def post_supply(supply: Supply, lines_data: list[dict], *, user=None) -> Supply:
    """Провести накладную: создать строки и поднять по ним склад."""
    if not lines_data:
        raise SupplyError("В накладной нет ни одной строки.")
    # Оплата без счёта раньше молча не попадала в кассу (аудит F1: в книге
    # 100 000 при реальных 68 000). Сервер не пропускает её, кто бы ни звал.
    if supply.is_opening and (supply.paid_amount or supply.paid_account):
        raise SupplyError(
            "Начальные остатки — это склад на дату переезда: оплаты по ним нет. "
            "Долг поставщику вносится отдельно, в его карточке."
        )
    if supply.paid_amount > 0 and supply.paid_account not in ("CASH", "BANK"):
        raise SupplyError("Укажите, чем платили: наличными или с банка.")

    happened_at = _moment(supply.received_on)
    reason_head = f"Накладная {supply.number}" if supply.number else "Приходная накладная"
    if supply.is_opening:
        reason_head = f"Начальные остатки {supply.number}".strip()
    if supply.supplier_id:
        reason_head += f" · {supply.supplier.name}"

    for data in lines_data:
        material = data["material"]
        form = data.get("form") or (
            SupplyLine.Form.SHEET if material.is_roll_material else SupplyLine.Form.QTY
        )
        cost = Decimal(str(data.get("cost") or 0))
        # Лист без размеров — берём размер листа с материала (форма его так и
        # подставляет; здесь то же для тех, кто зовёт API напрямую).
        if form == SupplyLine.Form.SHEET and material.is_roll_material:
            if not data.get("width") and material.sheet_width:
                data["width"] = material.sheet_width
            if not data.get("height") and material.sheet_height:
                data["height"] = material.sheet_height
        # Рулон без ширины — ширина из карточки (она для того и заведена):
        # партия замораживает её у себя, как и при быстром приходе.
        if form == SupplyLine.Form.ROLL and material.is_roll_material:
            if not data.get("width") and material.roll_width:
                data["width"] = material.roll_width
        qty = line_quantity(
            material, form,
            width=data.get("width"), height=data.get("height"),
            length=data.get("length"), sheet_count=data.get("sheet_count"),
            quantity=data.get("quantity"),
        )
        if qty <= 0:
            raise SupplyError(
                f"«{material.name}»: не из чего посчитать количество. "
                "Проверьте размеры или количество в строке."
            )

        line = SupplyLine(
            supply=supply, material=material, form=form,
            width=data.get("width"), height=data.get("height"),
            length=data.get("length"), sheet_count=data.get("sheet_count"),
            quantity=qty, cost=cost, cost_fc=data.get("cost_fc"),
            code=data.get("code", "") or "",
        )

        if material.is_roll_material and form != SupplyLine.Form.QTY:
            # Площадный материал приходит партией: у неё своя себестоимость, по
            # ней потом считаются FIFO и стоимость склада.
            line.roll = receive_lot(
                material,
                form=Roll.Form.SHEET if form == SupplyLine.Form.SHEET else Roll.Form.ROLL,
                width=data.get("width"), height=data.get("height"),
                length=data.get("length"), sheet_count=data.get("sheet_count"),
                purchase_cost=cost, code=line.code, user=user,
                received_at=happened_at, supply=supply,
            )
        elif not material.is_roll_material:
            # ШТУЧНЫЙ материал тоже приходит ПАРТИЕЙ (STK-01, как «Поступление»
            # одной кнопкой с 27.08): иначе накладная поднимала только общий
            # остаток, у проданного не было партии, а себестоимость брала
            # «последнюю закупочную» — склад показывал фантомную стоимость, а
            # прибыль расходилась с накладной. Остаток, лежавший до партий,
            # не трогаем: продажа берёт сначала партии (FIFO), потом остаток
            # без партии по закупочной цене из карточки — как и раньше.
            line.roll = receive_lot(
                material, form=Roll.Form.PIECE, sheet_count=qty,
                purchase_cost=cost, code=line.code, user=user,
                received_at=happened_at, supply=supply,
            )
        else:
            # Площадной материал, принятый «по количеству» (кв.м одним числом):
            # размеров нет, партии не из чего собрать — обычное движение склада.
            apply_stock_change(
                material, qty,
                log_type=InventoryLog.Type.SUPPLY,
                actual_price=(cost / qty).quantize(Decimal("0.01")) if qty else None,
                reason=f"{reason_head}: {material.name}",
                user=user, happened_at=happened_at, supply=supply,
            )

        line.save()

    # ОПЛАТА ПОСТАВЩИКУ — одной записью на весь документ, а не на каждую
    # строку: платят за накладную целиком, и в кассовой книге она должна
    # читаться так же. Оплата бывает частичной — берём ровно `paid_amount`,
    # остаток честно висит в `Supply.debt`. Счёт не выбран (взяли в долг) —
    # записи нет.
    if supply.paid_account and supply.paid_amount > 0 and not supply.is_opening:
        from finance import cash

        cash.supplier_paid(
            supply.paid_amount, supply.paid_account,
            supply=supply,
            happened_on=supply.received_on,
            note=reason_head,
            user=user,
        )

    return supply


@transaction.atomic
def move_supply_date(supply: Supply, day) -> None:
    """Перенести дату проведённой накладной вместе с её следом на складе.

    По дате накладной идут закуп месяца (`SupplyLine` → `received_on`), FIFO
    (`Roll.received_at`) и складской лист / журнал (`InventoryLog.happened_at`).
    Раньше правилась только сама дата: закуп уезжал в другой месяц, а партии и
    движения оставались в старом — финотчёт и складской лист расходились на
    сумму накладной, партия стояла в очереди FIFO не по своей дате.
    """
    moment = _moment(day)
    supply.received_on = day
    supply.save(update_fields=["received_on"])
    Roll.objects.filter(supply_line__supply=supply).update(received_at=moment)
    supply.inventory_logs.update(happened_at=moment)


@transaction.atomic
def pay_supply(supply: Supply, amount, account, *, paid_on=None, user=None,
               rate=None, note="", confirm_rate=False) -> Decimal:
    """Заплатить поставщику по накладной (часть долга или весь).

    Каждая оплата — ОТДЕЛЬНАЯ СТРОКА (`SupplierPayment`: дата, сумма, счёт,
    автор) и расход в кассу датой оплаты. Старые поля накладной («оплачено»,
    «чем») не трогаются — оплачено = старое поле + платежи. Накладная в валюте:
    сумма — в валюте, обязателен курс на день оплаты (`rate`).
    Возвращает остаток долга в сомах.
    """
    from .supplier_ledger import record_payment

    record_payment(
        supply=supply, amount=amount, account=account, paid_on=paid_on,
        rate=rate, note=note, user=user, confirm_rate=confirm_rate,
    )
    return Supply.objects.get(pk=supply.pk).debt


def sync_supply_payment(supply: Supply, *, old_amount, old_account, user=None) -> None:
    """Правку полей оплаты накладной довести до кассы.

    `paid_amount` правится и формой накладной; раньше касса об этом не знала.
    Разницу пишем движением сегодняшним днём: выросло — расход, уменьшилось —
    приход обратно (на тот счёт, откуда платили). Сменили счёт при уже
    уплаченной сумме — деньги переезжают со старого на новый. Старые приходы
    без счёта (до 19.09 оплаты в кассу не шли) уменьшение не трогает: из
    кассы по ним ничего не уходило.
    """
    from finance import cash
    from finance.models import CashEntry

    new_amount = supply.paid_amount or Decimal("0")
    new_account = supply.paid_account or ""
    old_amount = old_amount or Decimal("0")
    label = supply.number or f"#{supply.pk}"
    note = f"Правка оплаты накладной {label}"
    if old_account and new_account and old_account != new_account and old_amount > 0:
        cash.money_in(old_amount, CashEntry.Article.SUPPLY, account=old_account,
                      supply=supply, note=note, user=user)
        cash.money_out(old_amount, CashEntry.Article.SUPPLY, account=new_account,
                       supply=supply, note=note, user=user)
    delta = new_amount - old_amount
    if delta > 0:
        if not new_account:
            raise SupplyError("Укажите, чем платили: наличными или с банка.")
        cash.money_out(delta, CashEntry.Article.SUPPLY, account=new_account,
                       supply=supply, note=note, user=user)
    elif delta < 0:
        back_to = old_account or new_account
        if back_to and old_account:
            cash.money_in(-delta, CashEntry.Article.SUPPLY, account=back_to,
                          supply=supply, note=note, user=user)


def supply_summary(supply: Supply) -> str:
    """Накладная одной строкой — для журнала действий ПЕРЕД отменой: после неё
    от документа не остаётся ничего, и вопрос «что там было» отвечать нечем."""
    head = f"№{supply.number}" if supply.number else f"#{supply.pk}"
    if supply.supplier_id:
        head += f" от {supply.supplier.name}"
    head += f" ({supply.received_on:%d.%m.%Y})"
    lines = ", ".join(
        f"{line.material.name} × {line.quantity.normalize():f} на {line.cost.normalize():f} сом"
        for line in supply.lines.select_related("material")
    )
    tail = f"{head}, {supply.total_cost.normalize():f} сом" + (f" ({lines})" if lines else "")
    paid = supply.paid_total
    if paid:
        tail += f"; оплачено {paid.normalize():f}"
    return tail


@transaction.atomic
def unpost_supply(supply: Supply, *, user=None) -> None:
    """Отменить накладную: такой поставки не было — убрать её след целиком.

    Отменяем ТОЛЬКО нетронутую поставку: если из партии уже резали, откат
    сдвинул бы себестоимость закрытых заказов — тех самых, что уже посчитаны в
    прибыли. В таком случае честнее сказать «нельзя», чем тихо переписать
    прошлое (ровно по этой причине материал с продажами не удаляется, а
    прячется).

    Записи склада по накладной УДАЛЯЮТСЯ, а не остаются встречным движением —
    как у удалённого чека (`delete_receipt`): отмена — это исправление ошибки
    ввода, а не событие на складе. Раньше журнал оставался («поступление
    +29.77» и рядом «отмена −29.77»), а логи поступления отвязывались от
    документа — и дальше считались ОДИНОЧНЫМИ приходами: закуп месяца в
    финотчёте держал сумму отменённой накладной (35 000 сом) навсегда, а
    складской лист показывал её в «поступлении». Материал уходит с остатка
    без записи в журнал — приход и его отмена дают ноль. След остаётся в
    ЖУРНАЛЕ ДЕЙСТВИЙ, вместе с составом (см. `supply_summary` во вьюхе).
    """
    # Возвращённое поставщику датой возврата (RU-N23, D-171) ушло с полки
    # записью «Возврат поставщику» этой же накладной — это не «резали»: такая
    # запись уйдёт вместе с накладной, и снимаем с остатка только то, что ещё
    # лежит.
    returned = {}
    for row in SupplierReturnLine.objects.filter(supply_line__supply=supply, ret__in_place=False):
        returned[row.supply_line_id] = returned.get(row.supply_line_id, Decimal("0")) + row.area
    for line in supply.lines.select_related("material", "roll"):
        roll = line.roll
        back = returned.get(line.pk, Decimal("0"))
        if roll and roll.remaining_area + back != roll.initial_area:
            raise SupplyError(
                f"«{line.material.name}» из этой накладной уже резали — "
                "отменить её нельзя. Опечатку в цене или количестве поправит "
                "«Исправить приход», а часть товара можно вернуть поставщику."
            )
        material = line.material
        if not material.is_roll_material and material.quantity < line.quantity - back:
            raise SupplyError(
                f"«{material.name}»: на складе осталось меньше, чем пришло по "
                "накладной, — часть уже продали. Отменить нельзя."
            )

    from finance import cash

    label = supply.number or f"#{supply.pk}"
    for line in supply.lines.select_related("material", "roll"):
        material = line.material
        if line.roll:
            roll = line.roll
            line.roll = None
            line.save(update_fields=["roll"])
            cash.reverse_supplier_payments(roll=roll, note=f"Отмена накладной {label}")
            roll.delete()
        # Снимаем с остатка ровно то, что накладная принесла и что не уехало
        # назад поставщику, — без строки в журнале: её приход и возвраты тоже
        # уходят ниже, и движения в сумме нет.
        apply_stock_change(material, -(line.quantity - returned.get(line.pk, Decimal("0"))))
    supply.inventory_logs.all().delete()
    # Оплата поставщику НЕ стирается (2026-10-07, аудит Б-13): исходная запись
    # остаётся в книге, рядом — встречная сегодняшним днём. Раньше записи
    # системы удалялись, и деньги, отданные в прошлом месяце, исчезали из ОДДС
    # уже принятого месяца. Ручную запись не трогаем: её сделал человек.
    cash.reverse_supplier_payments(supply=supply, note=f"Отмена накладной {label}", user=user)
    # Платежи-строки уйдут вместе с накладной (их деньги вернула строка выше), а
    # курсовая разница — отдельной тратой — отменяется встречной.
    from .supplier_ledger import reverse_fx

    for payment in supply.payments.exclude(fx_diff=0):
        reverse_fx(payment, f"Отмена накладной {label}", user)
    supply.delete()


# Латинские буквы, которые на бумаге и на экране не отличить от кириллицы:
# «H-102» латиницей и «Н-102» кириллицей — один номер (F11/G3-N2).
_TWINS = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "ё": "е",
})
DUPLICATE_DAYS = 3


def number_key(number) -> str:
    """Номер накладной для сравнения: без регистра, пробелов и знаков
    («Э-102», «э 102», «Э102» — одно), латинские двойники — кириллицей."""
    text = (number or "").casefold().translate(_TWINS)
    return "".join(ch for ch in text if ch.isalnum())


def _looks_same(a_key, a_total, b_key, b_total) -> bool:
    """Оба с номером — по номеру; хоть у одной номера нет — по сумме до тыйына."""
    if a_key and b_key:
        return a_key == b_key
    return bool(a_total) and a_total > 0 and a_total == b_total


def find_duplicate(*, supplier_id, number, received_on, total, exclude_id=None):
    """Накладная, которая выглядит как повторный ввод этой же (F11, G3-N2).

    Совпадение — тот же поставщик, дата ± 3 дня и: номер (без регистра,
    пробелов и знаков, латиница = кириллица) или, если номера нет у одной из
    двух, сумма до тыйына. Двойной ввод (бумажную накладную внесли дважды)
    удваивает склад и закуп и молча остаётся: 20 листов на складе при десяти
    физических. Раньше ловилось только точное совпадение номера и даты —
    «H-102» латиницей, «Э102» без дефиса, дата на день позже и накладная без
    номера на ту же сумму проходили.
    """
    from datetime import timedelta

    qs = Supply.objects.filter(
        received_on__gte=received_on - timedelta(days=DUPLICATE_DAYS),
        received_on__lte=received_on + timedelta(days=DUPLICATE_DAYS),
    )
    qs = qs.filter(supplier_id=supplier_id) if supplier_id else qs.filter(supplier__isnull=True)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    key = number_key(number)
    total = Decimal(str(total or 0)).quantize(Decimal("0.01"))
    found = []
    for other in qs.select_related("supplier").prefetch_related("lines"):
        if _looks_same(key, total, number_key(other.number), other.total_cost):
            found.append(other)
    # Ближайшая по дате, при равенстве — введённая раньше.
    found.sort(key=lambda o: (abs((o.received_on - received_on).days), o.pk))
    return found[0] if found else None


def duplicate_map() -> dict:
    """{id накладной: id похожей на неё} для подсветки возможных дублей в списке
    — тем же правилом, что `find_duplicate`."""
    from django.db.models import Sum

    by_supplier: dict = {}
    for row in Supply.objects.annotate(total=Sum("lines__cost")).values(
        "id", "supplier_id", "number", "received_on", "total"
    ):
        by_supplier.setdefault(row["supplier_id"], []).append(
            (row["received_on"], row["id"], number_key(row["number"]),
             (row["total"] or Decimal("0")).quantize(Decimal("0.01")))
        )
    out = {}
    for rows in by_supplier.values():
        rows.sort()
        for i, (day, pk, key, total) in enumerate(rows):
            for day2, pk2, key2, total2 in rows[i + 1:]:
                if (day2 - day).days > DUPLICATE_DAYS:
                    break
                if _looks_same(key, total, key2, total2):
                    out.setdefault(pk, pk2)
                    out.setdefault(pk2, pk)
    return out
