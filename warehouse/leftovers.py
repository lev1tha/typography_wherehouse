"""Полка остатков (2026-10-11, D-200…D-205): положить, пересчитать, списать.

Остаток — кусок, уже списанный заказом (его материал сидит в себестоимости
заказа), поэтому здесь НЕТ ни склада, ни денег: ни `InventoryLog`, ни партий,
ни себестоимости. Только сколько кусков лежит и почему стало меньше или больше
— каждое изменение пишется в журнал действий «было → стало».

Сколько лежит, считается одним правилом (`resync`): положено − продано
(невозвращённые строки чеков со ссылкой на остаток) − списано. Так любое
движение по чеку — продажа, частичный возврат, удаление чека, отмена возврата,
правка состава — приводит полку к правде, не храня отдельного счётчика
«сколько вернули». Строку остатка перед этим блокируют
(`select_for_update(of=("self",))`, D-175): двое продавцов одного куска
выстраиваются в очередь, и второй видит продажу первого.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from audit.models import AuditLog

from .models import Leftover

ZERO = Decimal("0")


class LeftoverRejected(Exception):
    """Отказ полки — 400 с текстом для человека."""


def status_for(pieces: int, left: int, written_off: int) -> str:
    """Статус по счёту кусков. Списанный остаток, к которому вернулся
    проданный кусок, снова «на полке частично» — он физически лежит."""
    if left > 0:
        return Leftover.Status.ON_SHELF if left == pieces else Leftover.Status.PARTIAL
    return Leftover.Status.WRITTEN_OFF if written_off > 0 else Leftover.Status.SOLD


def _journal(user, leftover: Leftover, event: str, before: int, after: int) -> None:
    AuditLog.record(
        user,
        f"Полка: {leftover.label} — {event}: на полке было {before} → стало {after} шт",
        kind="stock",
    )


def check_size(measure: str, width, length) -> None:
    """Размер куска под мерку: кв.м — ширина и длина, пог.м — только длина."""
    def positive(v):
        return v is not None and Decimal(v) > 0

    if measure == Leftover.Measure.SQM and not (positive(width) and positive(length)):
        raise LeftoverRejected("У куска в кв.м укажите ширину и длину одного куска, м.")
    if measure == Leftover.Measure.METER:
        if width not in (None, ""):
            raise LeftoverRejected("У куска в пог.м ширины нет — укажите только длину.")
        if not positive(length):
            raise LeftoverRejected("У куска в пог.м укажите длину одного куска, м.")
    for value in (width, length):
        if value is not None and Decimal(value) <= 0:
            raise LeftoverRejected("Размер куска — больше нуля.")


@transaction.atomic
def put_on_shelf(*, material, measure, pieces, width=None, length=None, site=None,
                 source_receipt=None, note="", user=None) -> Leftover:
    """Положить куски на полку. Склада не трогает: материал уже списан заказом."""
    check_size(measure, width, length)
    if not pieces or int(pieces) < 1:
        raise LeftoverRejected("Сколько кусков кладёте на полку — хотя бы один.")
    pieces = int(pieces)
    leftover = Leftover.objects.create(
        material=material, measure=measure, pieces=pieces, pieces_left=pieces,
        width=width if measure != Leftover.Measure.METER else None, length=length,
        site=site, source_receipt=source_receipt, note=(note or "").strip()[:255],
        created_by=user,
    )
    origin = (
        f"положили (заказ №{source_receipt.order_number or source_receipt.pk})"
        if source_receipt is not None else "положили вручную"
    )
    if leftover.note:
        origin += f", {leftover.note}"
    _journal(user, leftover, origin, 0, pieces)
    return leftover


def sold_pieces(leftover_id) -> int:
    """Продано кусков: невозвращённые строки чеков со ссылкой на остаток."""
    from sales.models import TransactionItem

    total = TransactionItem.objects.filter(
        leftover_id=leftover_id, is_returned=False,
    ).aggregate(v=Sum("quantity"))["v"] or ZERO
    return int(total)


@transaction.atomic
def lock_for_sale(leftover_id, pieces) -> Leftover:
    """Замок строки остатка перед продажей и проверка «столько лежит».

    `of=("self",)` — FOR UPDATE только этой таблицы (D-175): Postgres не
    блокирует строку на «нулевой» стороне outer join, а `select_related` к
    nullable-ссылкам такой join и даёт.
    """
    try:
        leftover = (
            Leftover.objects.select_for_update(of=("self",))
            .select_related("material").get(pk=leftover_id)
        )
    except Leftover.DoesNotExist:
        raise LeftoverRejected("Остаток не найден на полке.")
    pieces = Decimal(str(pieces or 0))
    if pieces <= 0 or pieces != pieces.to_integral_value():
        raise LeftoverRejected(f"{leftover.label}: с полки продают целыми кусками — укажите их число.")
    if pieces > leftover.pieces_left:
        raise LeftoverRejected(
            f"{leftover.label}: на полке {leftover.pieces_left} шт — продать {int(pieces)} нельзя."
        )
    return leftover


@transaction.atomic
def resync(leftover_ids, *, user=None, event: str = "") -> list:
    """Пересчитать, сколько лежит на полке, по строкам чеков — и записать
    «было → стало», если изменилось. Меньше нуля — отказ (`LeftoverRejected`):
    один и тот же кусок продан дважды. Вызывать В ТОЙ ЖЕ транзакции, что и
    правка строк: отказ откатывает её целиком.
    """
    ids = sorted({i for i in leftover_ids if i})
    if not ids:
        return []
    changed = []
    # Порядок по id — две параллельные продажи двух одинаковых кусков не
    # заблокируют друг друга крест-накрест.
    for leftover in (
        Leftover.objects.select_for_update(of=("self",))
        .select_related("material").filter(pk__in=ids).order_by("pk")
    ):
        before = leftover.pieces_left
        after = leftover.pieces - sold_pieces(leftover.pk) - leftover.written_off_pieces
        if after < 0:
            raise LeftoverRejected(
                f"{leftover.label}: на полке {max(before, 0)} шт — столько уже продано, "
                f"второй раз этот кусок не продать."
            )
        status = status_for(leftover.pieces, after, leftover.written_off_pieces)
        if after != before or status != leftover.status:
            leftover.pieces_left = after
            leftover.status = status
            leftover.save(update_fields=["pieces_left", "status"])
            changed.append(leftover)
        if after != before:
            _journal(user, leftover, event or "пересчёт", before, after)
    return changed


@transaction.atomic
def write_off(leftover_id, *, reason: str, user=None) -> Leftover:
    """Списать (выбросили) всё, что лежит. Склада и денег не касается —
    материал списан давно; это запись «куска больше нет на полке»."""
    reason = (reason or "").strip()
    if not reason:
        raise LeftoverRejected("Укажите причину списания — она попадёт в журнал.")
    leftover = (
        Leftover.objects.select_for_update(of=("self",))
        .select_related("material").get(pk=leftover_id)
    )
    before = leftover.pieces_left
    if before <= 0:
        raise LeftoverRejected(f"{leftover.label}: на полке ничего не лежит — списывать нечего.")
    leftover.written_off_pieces += before
    leftover.pieces_left = 0
    leftover.status = status_for(leftover.pieces, 0, leftover.written_off_pieces)
    leftover.written_off_at = timezone.now()
    leftover.written_off_by = user
    leftover.write_off_reason = reason[:255]
    leftover.save(update_fields=[
        "written_off_pieces", "pieces_left", "status", "written_off_at", "written_off_by",
        "write_off_reason",
    ])
    _journal(user, leftover, f"списали (выбросили): {leftover.write_off_reason}", before, 0)
    return leftover
