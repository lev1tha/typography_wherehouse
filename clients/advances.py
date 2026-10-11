"""Аванс клиента без заказа и зачёт его (и сдачи) в оплату долга (CLI-05, D-93).

Что такое аванс. Клиент вносит деньги «на будущие работы»: заказа ещё нет.
В кассе это приход (статья «Оплата от клиента», без чека), у клиента — сальдо в
его пользу. Выручки нет: она появится, когда аванс зачтут в оплату заказа.
Сдача и аванс — одно и то же по смыслу (деньги клиента у нас), но хранятся
по-разному: сдача лежит на чеке (`Receipt.change_due`), аванс — отдельной
записью `ClientAdvance`, потому что чека у него нет.

Зачёт. Аванс гасит долг заказа без движения кассы — деньги лежат в ней с того
дня, как их внесли. Чеку добавляется `amount_paid` и `change_applied` (как при
зачёте сдачи), аванс уменьшается, а `BalanceOffset` помнит день и заказ — по
нему акт сверки видит зачёт.

Контракт для кассы (продажи). Чтобы потратить аванс на НОВЫЙ заказ, оформление
вызывает `advance_available(client)` и `take_advance(client, amount, receipt=…)`
сразу после зачёта сдачи (`_take_client_change`): функция уменьшает аванс,
пишет `BalanceOffset` и возвращает, сколько реально взято; `amount_paid` и
`change_applied` чека вызывающий увеличивает сам, как делает со сдачей.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import BalanceOffset, ClientAdvance

ZERO = Decimal("0")


class AdvanceRejected(Exception):
    """Аванс принять/отменить нельзя. Текст уходит пользователю как есть."""


def _checked(amount) -> Decimal:
    """Сумма аванса: больше нуля, меньше 10^10, не больше двух знаков (RM-N9)."""
    from .amounts import AmountRejected, check_amount

    amount = Decimal(str(amount))
    if not amount.is_finite() or amount <= 0:
        raise AdvanceRejected("Сумма аванса должна быть больше нуля.")
    try:
        return check_amount(amount, "Сумма аванса")
    except AmountRejected as e:
        raise AdvanceRejected(str(e))


def advance_available(client) -> Decimal:
    """Сколько аванса клиента ещё не зачтено."""
    if client is None:
        return ZERO
    total = ClientAdvance.objects.filter(client=client, reverted_at__isnull=True).aggregate(
        v=Sum("remaining")
    )["v"]
    return total or ZERO


@transaction.atomic
def accept_advance(client, amount, *, method="CASH", paid_on: date | None = None, note="", user=None):
    """Принять аванс: запись аванса + приход в кассу."""
    from finance import cash
    from finance.models import CashEntry

    amount = _checked(amount)
    method = str(method or "CASH").upper()
    if method not in ClientAdvance.Method.values:
        raise AdvanceRejected(
            "Способ оплаты: " + ", ".join(ClientAdvance.Method.values) + "."
        )
    day = paid_on or timezone.localdate()
    if day > timezone.localdate():
        raise AdvanceRejected("Дата оплаты не может быть в будущем.")
    advance = ClientAdvance.objects.create(
        client=client, amount=amount, remaining=amount, method=method, paid_on=day,
        note=(note or "")[:255], created_by=user,
    )
    cash.money_in(
        amount, CashEntry.Article.SALE, payment_method=method, happened_on=day, user=user,
        note=f"Аванс клиента «{client.display_name}»",
    )
    return advance


@transaction.atomic
def accept_advance_or_offset(client, amount, *, method="CASH", paid_on: date | None = None,
                             note="", user=None, offset_debt=True) -> dict:
    """«Принять аванс» с зачётом в живой долг (RM-N6, D-165).

    Клиент с долгом принёс деньги «вперёд» — по умолчанию они СНАЧАЛА гасят его
    долг, как общая выплата: входящий долг первым, потом заказы от старых к
    новым (те же `pay_opening_debts` и `pay_client_debt`, те же записи оплат и
    кассы). Авансом остаётся только то, что больше долга. `offset_debt=False` —
    как раньше: всё уходит в аванс, долг висит до зачёта.

    Возвращает `{"advance", "to_debt", "opening", "receipts"}`: аванс (или None,
    если всё ушло в долг), сколько ушло в долг и разнос по остаткам/заказам.
    """
    from sales.models import Receipt
    from sales.sale_service import pay_client_debt

    from .opening import open_debts_qs, pay_opening_debts

    amount = _checked(amount)
    method = str(method or "CASH").upper()
    if method not in ClientAdvance.Method.values:
        raise AdvanceRejected("Способ оплаты: " + ", ".join(ClientAdvance.Method.values) + ".")
    day = paid_on or timezone.localdate()
    if day > timezone.localdate():
        raise AdvanceRejected("Дата оплаты не может быть в будущем.")

    left = amount
    opening, receipts = [], []
    if offset_debt:
        if open_debts_qs().filter(client=client).exists():
            opening, left = pay_opening_debts(
                client, left, user=user, paid_on=paid_on, method=method, note=note,
            )
        owing = sum((r.debt for r in Receipt.objects.filter(client=client) if r.debt > 0), ZERO)
        take = min(left, owing)
        if take > 0:
            receipts, _change = pay_client_debt(
                client, take, user=user, paid_on=paid_on, method=method, note=note,
            )
            left -= sum((a for _r, a in receipts), ZERO)
    advance = None
    if left > 0:
        advance = accept_advance(client, left, method=method, paid_on=paid_on, note=note, user=user)
    return {
        "advance": advance,
        "to_debt": amount - left,
        "opening": opening,
        "receipts": receipts,
    }


@transaction.atomic
def revert_advance(advance: ClientAdvance, *, user=None) -> ClientAdvance:
    """Отменить ошибочный аванс целиком — пока из него ничего не зачтено.

    Кассовая книга не подчищается: приход остаётся, рядом встречный расход
    сегодняшним днём (как при откате оплаты).
    """
    from finance import cash
    from finance.models import CashEntry

    advance = ClientAdvance.objects.select_for_update().get(pk=advance.pk)
    if advance.reverted_at:
        raise AdvanceRejected("Этот аванс уже отменён.")
    if advance.remaining != advance.amount:
        raise AdvanceRejected(
            "Из этого аванса уже зачтено в оплату заказов — отменить его целиком нельзя."
        )
    advance.reverted_at = timezone.now()
    advance.remaining = ZERO
    advance.save(update_fields=["reverted_at", "remaining"])
    if advance.is_opening:
        # Входящий аванс (волна 2) в кассу не приходил — и уходить ему неоткуда;
        # запись входящего остатка отменяется вместе с ним.
        from .models import OpeningBalance

        OpeningBalance.objects.filter(advance=advance, reverted_at__isnull=True).update(
            reverted_at=advance.reverted_at,
        )
        return advance
    cash.money_out(
        advance.amount, CashEntry.Article.UNPAY, payment_method=advance.method, user=user,
        note=f"Отмена аванса клиента «{advance.client.display_name}»",
    )
    return advance


@transaction.atomic
def take_advance(client, amount, *, receipt=None, user=None, used_on: date | None = None) -> Decimal:
    """Списать до `amount` из авансов клиента (старые первыми). Возвращает взятое.

    Чек не трогает — только авансы и запись `BalanceOffset` (см. модульную
    строку: контракт для кассы).
    """
    left = Decimal(str(amount or 0))
    taken = ZERO
    if client is None or left <= 0:
        return taken
    day = used_on or timezone.localdate()
    qs = (
        ClientAdvance.objects.select_for_update()
        .filter(client=client, reverted_at__isnull=True, remaining__gt=0)
        .order_by("paid_on", "id")
    )
    for adv in qs:
        if left <= 0:
            break
        part = min(adv.remaining, left)
        adv.remaining -= part
        adv.save(update_fields=["remaining"])
        BalanceOffset.objects.create(
            client=client, receipt=receipt, order_number=getattr(receipt, "order_number", None),
            source=BalanceOffset.Source.ADVANCE, advance=adv, amount=part, used_on=day,
            created_by=user,
        )
        left -= part
        taken += part
    return taken


@transaction.atomic
def release_receipt_advance(receipt) -> Decimal:
    """Вернуть в авансы клиента всё, что из них зачтено в этот заказ (волна 2).

    Заказ удаляют или откатывают его оплату — аванс, потраченный на него,
    снова «не зачтён»: `remaining` растёт обратно у тех же авансов, записи
    зачёта уходят. Без этого аванс превращался в сдачу на другом заказе, а у
    клиента без других заказов — пропадал вместе с удалённым чеком.
    Возвращает, сколько вернулось.
    """
    if receipt is None or receipt.pk is None:
        return ZERO
    back = ZERO
    offsets = BalanceOffset.objects.select_for_update().filter(
        receipt=receipt, source=BalanceOffset.Source.ADVANCE, advance__isnull=False,
    )
    for o in offsets:
        adv = ClientAdvance.objects.select_for_update().get(pk=o.advance_id)
        adv.remaining = min(adv.amount, adv.remaining + o.amount)
        adv.save(update_fields=["remaining"])
        back += o.amount
        o.delete()
    return back


@transaction.atomic
def offset_debts(client, *, receipt_ids=None, user=None, paid_on: date | None = None, note=""):
    """Закрыть долги клиента его же деньгами: сначала сдача, потом аванс.

    Идём от старых заказов к новым, как и общая выплата. Касса не двигается.
    Возвращает `(по заказам, сдачей, авансом)`: по заказам — список
    `(чек, сумма)`.
    """
    from sales.models import Receipt
    from sales.sale_service import PaymentRejected, apply_payment, lock_receipt, receipt_owed

    wanted = {str(x) for x in receipt_ids} if receipt_ids is not None else None
    locked = list(Receipt.objects.select_for_update().filter(client=client).order_by("pk"))
    debts = sorted(
        (r for r in locked if r.debt > 0 and (wanted is None or str(r.id) in wanted)),
        key=lambda r: (r.created_at, str(r.pk)),
    )
    per_receipt: dict = {}
    via_change = via_advance = ZERO
    day = paid_on or timezone.localdate()
    for receipt in debts:
        got = ZERO
        # 1. Сдача с других заказов клиента — сервис продаж (`Payment` «Зачёт сдачи»).
        before = receipt.change_applied
        try:
            apply_payment(
                receipt, ZERO, user=user, paid_on=day, use_change=True,
                note=note or "Зачтено из сдачи клиента",
            )
        except PaymentRejected:      # «по этому чеку долга нет» — для зачёта не ошибка
            pass
        receipt.refresh_from_db()
        part = receipt.change_applied - before
        got += part
        via_change += part
        # 2. Аванс.
        need = receipt_owed(receipt)
        if need > 0 and advance_available(client) > 0:
            lock_receipt(receipt)
            need = receipt_owed(receipt)
            take = take_advance(client, need, receipt=receipt, user=user, used_on=day)
            if take > 0:
                receipt.amount_paid += take
                receipt.change_applied += take
                if receipt.amount_paid >= receipt.total_price - receipt.refunded_amount:
                    receipt.payment_status = Receipt.PaymentStatus.PAID
                receipt.save(update_fields=["amount_paid", "change_applied", "payment_status", "updated_at"])
                got += take
                via_advance += take
        if got > 0:
            per_receipt[receipt.pk] = (receipt, got)
    return list(per_receipt.values()), via_change, via_advance
