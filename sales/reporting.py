"""Деньги периода по чекам — одно правило для всех отчётов.

ПРОДАЖА относится к дню ПРИЗНАНИЯ ВЫРУЧКИ (`Receipt.revenue_recognized_at`), ВОЗВРАТ —
к дню, когда его оформили (решение владельца, 2026-09-27). У обычного заказа день
признания — день заказа; у онлайн-заказа — день подтверждения оплаты, а
неоплаченный онлайн-счёт в выручку не входит вовсе (2026-10-07, D-7/D-14). Раньше возврат вычитался из месяца заказа: отчёт,
который уже посмотрели и приняли, назавтра показывал другую выручку, а по
заказу из закрытого месяца возврат не оформлялся вовсе, пока период не
откроешь.

Считается это так. Основа — как и раньше: заказы периода, кроме отменённых,
«итог − возвращено» (у строк — невозвращённые). К ней две поправки по строкам,
у которых записана дата возврата (`TransactionItem.returned_at`):

- строка заказа ЭТОГО периода, возвращённая когда угодно, добавляется обратно:
  в день заказа это была продажа (основа её уже выкинула);
- строка, возвращённая В ЭТОМ периоде, из какого угодно заказа, вычитается.

Возврат внутри периода даёт «+ и −» — то же, что основа без поправок. Возврат
позже периода периода не трогает. Возврат по заказу прошлого периода
уменьшает текущий. За всю историю выходит ровно «итог − возвращено» по
неотменённым заказам, как и было.

Строки без даты возврата (вернули до появления поля; миграция проставила им
дату заказа) поправок не дают — для них всё как раньше.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Receipt, TransactionItem

ZERO = Decimal("0")

# Дата, по которой продажа ложится в отчёты, — у чека и у строк чека.
SOLD_ON = "revenue_recognized_at"
LINE_SOLD_ON = "receipt__revenue_recognized_at"


def sold_receipts(qs=None):
    """Чеки, ставшие продажей: не отменённые и с датой признания."""
    qs = Receipt.objects.all() if qs is None else qs
    return qs.exclude(status=Receipt.Status.CANCELLED).filter(revenue_recognized_at__isnull=False)


def sold_lines(qs=None):
    """Строки таких чеков."""
    qs = TransactionItem.objects.all() if qs is None else qs
    return qs.exclude(receipt__status=Receipt.Status.CANCELLED).filter(
        receipt__revenue_recognized_at__isnull=False
    )


def day_start(day):
    """Начало суток `day` по местному времени (Asia/Bishkek) — момент, а не дата.

    Фильтр `поле__date` заставляет базу приводить КАЖДУЮ строку к местной дате,
    и индекс по колонке молчит; границы суток считаем один раз здесь и режем
    колонку диапазоном. Набор строк тот же: запись принадлежит тем суткам, в
    которые она произошла по местному времени.
    """
    return timezone.make_aware(
        datetime.combine(day, time.min), timezone.get_current_timezone()
    )


def day_after(day):
    """Начало суток, следующих за `day` (правая граница диапазона, не входит)."""
    return day_start(day + timedelta(days=1))


def _between(qs, field, d_from, d_to):
    if d_from:
        qs = qs.filter(**{f"{field}__gte": day_start(d_from)})
    if d_to:
        qs = qs.filter(**{f"{field}__lt": day_after(d_to)})
    return qs


def _dated_returns():
    return TransactionItem.objects.filter(
        is_returned=True, returned_at__isnull=False, receipt__revenue_recognized_at__isnull=False,
    )


def added_back(d_from=None, d_to=None):
    """Возвращённые строки продаж периода — в периоде продажи они продажа."""
    return _between(_dated_returns(), LINE_SOLD_ON, d_from, d_to)


def returned_lines(d_from=None, d_to=None):
    """Строки, возвращённые в периоде (по дате возврата), из любых заказов."""
    return _between(_dated_returns(), "returned_at", d_from, d_to)


def prior_returns(d_from, d_to):
    """Возвращённые в периоде строки заказов ПРОШЛЫХ периодов.

    Разбивки по заказам периода (станок, материал, клиент) их не видят —
    а выручку периода они уменьшают. Без начала периода прошлых заказов нет.
    """
    if not d_from:
        return TransactionItem.objects.none()
    return returned_lines(d_from, d_to).filter(**{f"{LINE_SOLD_ON}__lt": day_start(d_from)})


def counts_at(item, d_to) -> bool:
    """Входит ли строка заказа периода в его разбивку на конец периода.

    Невозвращённая — да, если заказ не отменён. Возвращённая — только если её
    вернули ПОЗЖЕ периода: тогда в периоде она была продажей. Нужен
    `select_related("receipt")` или загруженный чек.
    """
    if item.receipt.revenue_recognized_at is None:
        return False      # неоплаченный онлайн-счёт — ещё не продажа
    if item.is_returned:
        if d_to is None or item.returned_at is None:
            return False
        return timezone.localtime(item.returned_at).date() > d_to
    return item.receipt.status != Receipt.Status.CANCELLED


def money(lines) -> Decimal:
    """Стоимость строк, как она стояла в чеке, — вверх до сома по каждой
    (`TransactionItem.sold_total`), в том числе у возвращённых: возвращают
    ровно то, что продали. Считаем в Python, а не суммой в базе — SQLite
    умножает в double и даёт копеечный шум."""
    return sum(
        (it.sold_total for it in lines.only("quantity", "price_per_item")),
        ZERO,
    )


def cost(lines) -> Decimal:
    """Себестоимость строк — снимок на момент списания со склада."""
    return lines.aggregate(
        v=Coalesce(Sum("cost_total"), ZERO, output_field=DecimalField())
    )["v"]


def _SUM(field):
    return Coalesce(Sum(field), ZERO, output_field=DecimalField())


def revenue(d_from=None, d_to=None, receipts=None) -> Decimal:
    """Выручка периода. `receipts` — сузить до части чеков (способ оплаты)."""
    base_qs = _between(sold_receipts(receipts), SOLD_ON, d_from, d_to)
    agg = base_qs.aggregate(t=_SUM("total_price"), r=_SUM("refunded_amount"))
    back = added_back(d_from, d_to)
    out = returned_lines(d_from, d_to)
    if receipts is not None:
        back = back.filter(receipt__in=receipts)
        out = out.filter(receipt__in=receipts)
    return agg["t"] - agg["r"] + money(back) - money(out)


def cogs(d_from=None, d_to=None) -> Decimal:
    base = _between(sold_lines(TransactionItem.objects.filter(is_returned=False)), LINE_SOLD_ON, d_from, d_to)
    return cost(base) + cost(added_back(d_from, d_to)) - cost(returned_lines(d_from, d_to))


def refunds(d_from=None, d_to=None) -> Decimal:
    """Сколько возвратов оформлено в периоде — по стоимости строк."""
    return money(returned_lines(d_from, d_to))


def by_day(d_from, d_to):
    """Выручка и себестоимость по дням: ({дата: выручка}, {дата: себестоимость}).

    Продажа — днём признания выручки, возврат — днём возврата (местное время)."""
    rev = defaultdict(lambda: ZERO)
    cst = defaultdict(lambda: ZERO)

    def day_of(moment):
        return timezone.localtime(moment).date()

    for r in _between(sold_receipts(), SOLD_ON, d_from, d_to).only(
        SOLD_ON, "total_price", "refunded_amount"
    ):
        rev[day_of(r.revenue_recognized_at)] += r.total_price - r.refunded_amount
    for it in _between(
        sold_lines(TransactionItem.objects.filter(is_returned=False)), LINE_SOLD_ON, d_from, d_to,
    ).select_related("receipt").only("cost_total", LINE_SOLD_ON):
        cst[day_of(it.receipt.revenue_recognized_at)] += it.cost_total
    for it in added_back(d_from, d_to).select_related("receipt").only(
        "quantity", "price_per_item", "cost_total", LINE_SOLD_ON
    ):
        rev[day_of(it.receipt.revenue_recognized_at)] += it.sold_total
        cst[day_of(it.receipt.revenue_recognized_at)] += it.cost_total
    for it in returned_lines(d_from, d_to).only(
        "quantity", "price_per_item", "cost_total", "returned_at"
    ):
        rev[day_of(it.returned_at)] -= it.sold_total
        cst[day_of(it.returned_at)] -= it.cost_total
    return rev, cst
