"""Акт сверки взаиморасчётов — считает сервер (CLI-04, D-90).

Раньше акт собирался в браузере из карточки клиента, и входящее сальдо было
зашито 0,00: за III квартал он печатал долг 5 963 вместо 19 963, а отрицательное
сальдо — без знака. Теперь выписка строится здесь, один раз и для всех:
кабинета клиента, печати и тестов.

Знак сальдо: плюс — клиент должен нам («долг клиента»), минус — мы должны
клиенту («аванс клиента»: переплата, сдача, принятый аванс).

Откуда строки. В системе нет проводок — есть состояние чека (итог, оплачено,
возвращено, сдача) и несколько датированных записей: оплаты долга (`Payment`),
возвраты строк (`returned_at`), выданная сдача (кассовая книга), зачёты
(`BalanceOffset`), авансы (`ClientAdvance`). Строки собираются так, чтобы сумма
всех чеков клиента СХОДИЛАСЬ с долгом карточки по построению, а не по
совпадению: у каждого чека

    дебет − кредит = его долг − его сдача,

и деньги «при оформлении» — это остаток, который вычисляется из этого равенства.
Поэтому после правки состава вниз, отката оплаты, возврата и выдачи сдачи акт
без дат всегда равен `долг − сдача − аванс` из карточки (раньше расходился:
4 453 против 5 753). Даты у недатированных частей — день заказа.

Зачёт сдачи в долг другого заказа (`Payment` «Зачёт сдачи», `change_applied`) —
деньги, которые клиент уже принёс раньше. Чтобы сальдо на любую дату было
верным (клиент внёс 100 000 в августе, заказ на 45 000 появился в сентябре — на
31 августа мы ему должны 70 000, а не 25 000), деньги возвращаются в день их
внесения, а в день зачёта идёт нейтральная пара строк «списано из сдачи» /
«оплата зачётом». Какой заказ чья сдача, система не хранит — источник берём из
кассовой книги: в ящике по чеку лежит больше, чем он должен держать
(`held − (оплачено − зачтено + сдача)`) — ровно столько у него «забрали»
(так же определяет это `_ensure_change_not_spent`). Нет следа в кассе (заказы до
кассовой книги) — зачёт остаётся днём зачёта, и история до него приблизительна;
сальдо «на сегодня» от этого не зависит.

Отмена оплаты, откат оплаты и уменьшение списания возвратом (D-155, D-158)
записей не удаляют: оплата остаётся своим днём, а отмена — встречной записью
`Payment` с минусом днём отмены (строка `payment_cancelled` / `write_off_cancelled`,
дебет). Сумма записей по чеку от этого «чистая», и «принесено при оформлении»
не меняется — акт прошлого (в том числе закрытого) месяца не переписывается.

Что в акте не участвует: заказы без признанной выручки (неоплаченный онлайн-счёт,
D-7/D-37 — «не продажа и не долг»; тот же отбор, что у долга карточки).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone

ZERO = Decimal("0")

# Порядок строк внутри одного дня: входящий остаток, заказ, возвраты, оплаты, остальное.
RANK = {
    "opening_debt": -2, "opening_advance": -1,
    "opening_payment": 5, "opening_write_off": 6,
    "opening_payment_cancelled": 6, "opening_write_off_cancelled": 6,
    "order": 0, "change_applied": 1, "refund": 2, "refund_paid": 3, "paid_upfront": 4,
    "payment": 5, "write_off": 6, "offset": 7, "advance_used": 7, "change_given": 8,
    "payment_cancelled": 8, "write_off_cancelled": 8, "offset_cancelled": 8,
    "advance": 9, "advance_reverted": 10, "adjustment": 11,
}

LABELS = {
    "debt": "Долг клиента",
    "advance": "Аванс клиента (мы должны)",
    "zero": "Задолженности нет",
}


def side_of(balance: Decimal) -> str:
    if balance > 0:
        return "debt"
    if balance < 0:
        return "advance"
    return "zero"


def _day(moment) -> date:
    return timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()


def _row(day, kind, *, debit=ZERO, credit=ZERO, receipt=None, order_number=None, title="", **extra):
    return {
        "date": day, "kind": kind,
        "order_number": order_number if order_number is not None else (
            receipt.order_number if receipt is not None else None
        ),
        "title": title or (receipt.title if receipt is not None else ""),
        "debit": debit, "credit": credit, **extra,
    }


def _receipt_rows(receipt, offsets, given, held) -> tuple[list[dict], dict]:
    """Строки одного чека (см. модульную строку: дебет − кредит = долг − сдача)
    и сведения для зачётов: сколько у чека «забрали» сдачи (`moved_out`) и какие
    зачёты сдачи в него были (`moves_in`: день и сумма)."""
    recognized = _day(receipt.revenue_recognized_at)
    T, R, P = receipt.total_price, receipt.refunded_amount, receipt.amount_paid
    A, C, D = receipt.change_applied, receipt.change_due, receipt.debt
    rows = [_row(recognized, "order", debit=T, receipt=receipt)]

    # Возврат товара — в день возврата строки; кто не помнит дату — день заказа.
    by_day: dict[date, Decimal] = {}
    last_return = None
    for item in receipt.items.all():
        if not item.is_returned:
            continue
        day = _day(item.returned_at) if item.returned_at else recognized
        by_day[day] = by_day.get(day, ZERO) + item.sold_total
        last_return = day if last_return is None or day > last_return else last_return
    listed = sum(by_day.values(), ZERO)
    if R != listed:
        # Возврат записан суммой без строк (старые данные): разницу — на день заказа.
        by_day[recognized] = by_day.get(recognized, ZERO) + (R - listed)
        last_return = last_return or recognized
    for day, value in sorted(by_day.items()):
        if value:
            rows.append(_row(day, "refund", credit=value, receipt=receipt))

    # Деньги, отданные клиенту за возвращённое: сверх стоимости оставшегося.
    paid_back = max(ZERO, P - (T - R)) if R > 0 else ZERO
    if paid_back > 0:
        rows.append(_row(last_return or recognized, "refund_paid", debit=paid_back, receipt=receipt))

    # Оплаты долга, зачёты сдачи и списания — датированные записи `Payment`.
    # Зачёт сдачи (`CHANGE`) сидит и в `change_applied` чека — не считаем его
    # второй раз, когда ниже ищем недатированную часть.
    paid_later = ZERO
    payment_change = ZERO
    written_off = ZERO
    moves_in: list[dict] = []
    for p in receipt.payments.all():
        paid_later += p.amount
        method = str(p.method)
        if method == "WRITE_OFF":
            kind = "write_off"
            written_off += p.amount
        elif method == "CHANGE":
            kind = "offset"
            payment_change += p.amount
        else:
            kind = "payment"
        if p.amount < 0:
            # Встречная запись (D-155, D-158): отмена оплаты, откат, списание,
            # уменьшенное возвратом, — дебет днём отмены. Сама оплата осталась
            # своим днём: акт прошлого (и закрытого) месяца не переписывается.
            rows.append(_row(
                p.paid_on, f"{kind}_cancelled", debit=-p.amount, receipt=receipt,
                method=method, method_display=p.get_method_display(), note=p.note,
            ))
            continue
        row = _row(
            p.paid_on, kind, credit=p.amount, receipt=receipt,
            method=method, method_display=p.get_method_display(), note=p.note,
            **({"source": "CHANGE"} if kind == "offset" else {}),
        )
        rows.append(row)
        if kind == "offset":
            moves_in.append(row)

    # Зачёты сдачи/аванса в оплату этого заказа через `BalanceOffset`:
    # датированные — отдельно, остальное (зачёт при оформлении заказа) — днём заказа.
    dated = ZERO
    for o in offsets:
        dated += o.amount
        if o.source == "CHANGE":
            row = _row(o.used_on, "offset", credit=o.amount, receipt=receipt, source=o.source)
            rows.append(row)
            moves_in.append(row)
        # Зачёт аванса денег в акте не двигает: аванс уже записан при внесении,
        # а дебет «списано из аванса» и кредит «оплата зачётом» гасят друг
        # друга. Но видно его должно быть (волна 2): строка-пояснение с суммой
        # `amount`, дебет и кредит — ноль, сальдо она не меняет.
        else:
            rows.append(_row(o.used_on, "advance_used", receipt=receipt, amount=o.amount))
    undated = max(ZERO, A - dated - payment_change)
    if undated > 0:
        row = _row(recognized, "change_applied", credit=undated, receipt=receipt)
        rows.append(row)
        moves_in.append(row)
    applied_total = dated + undated

    # Сдача, выданная на руки: дебет в её день, а вместе с ней — и в «принято».
    given_total = ZERO
    for day, value in given:
        given_total += value
        rows.append(_row(day, "change_given", debit=value, receipt=receipt))

    # Принесено при оформлении — то, что осталось от равенства. `given_total`
    # прибавлен в обе стороны: в «принято» и в дебет «выдано».
    brought = T - R + paid_back + given_total - applied_total - paid_later - (D - C)
    upfront = None
    if brought > 0:
        upfront = _row(recognized, "paid_upfront", credit=brought, receipt=receipt)
        rows.append(upfront)
    elif brought < 0:
        # Данные разошлись (оплату сняли, а запись осталась): честная строка
        # вместо молчаливого искажения сальдо.
        rows.append(_row(recognized, "adjustment", debit=-brought, receipt=receipt))

    # Сколько сдачи у этого чека «забрали» другие заказы: в кассе по нему лежит
    # больше, чем он держит сам. Ушедшее на возврат и списание из «держит» вычтено.
    claim = P - A - written_off + C - paid_back
    moved_out = max(ZERO, held - claim) if held is not None else ZERO
    return rows, {
        "moved_out": moved_out, "moves_in": moves_in, "receipt": receipt, "day": recognized,
        "upfront": upfront, "rows": rows,
    }


def _return_moved_change(metas) -> None:
    """Сдача, зачтённая в другие заказы, возвращается в день внесения денег.

    Сколько ушло в зачёт (`moves_in` — строки-зачёты) и откуда (`moved_out` по
    чекам) — две стороны одного и того же; кассовая книга может знать меньше,
    чем система (старые заказы), поэтому переносим min(того, другого). Зачёты
    гасим от старых к новым — так сдачу и тратят (`_take_client_change`).
    Строка зачёта уменьшается на перенесённое (ноль — строка уходит из акта),
    источнику возвращается то же в «принято при оформлении»: сальдо не меняется,
    меняются только даты.
    """
    events = sorted(
        (row for m in metas for row in m["moves_in"]), key=lambda r: (r["date"], r["order_number"] or 0)
    )
    total_in = sum((r["credit"] for r in events), ZERO)
    total_out = sum((m["moved_out"] for m in metas), ZERO)
    left = min(total_in, total_out)
    if left <= 0:
        return
    budget = left
    for row in events:
        take = min(row["credit"], budget)
        row["credit"] -= take
        budget -= take
    budget = left
    for m in sorted(metas, key=lambda m: (m["day"], m["receipt"].pk)):
        take = min(m["moved_out"], budget)
        if take <= 0:
            continue
        budget -= take
        if m["upfront"] is not None:
            m["upfront"]["credit"] += take
        else:
            row = _row(m["day"], "paid_upfront", credit=take, receipt=m["receipt"])
            m["upfront"] = row
            m["rows"].append(row)


def statement_rows(client) -> list[dict]:
    """Все строки акта клиента за всё время, по порядку, без сальдо."""
    from finance.models import CashEntry

    from .models import BalanceOffset, ClientAdvance

    receipts = list(
        client.receipts.filter(revenue_recognized_at__isnull=False)
        .prefetch_related("items", "payments")
        .order_by("revenue_recognized_at", "pk")
    )
    ids = [r.pk for r in receipts]
    offsets: dict = {}
    for o in BalanceOffset.objects.filter(client=client, receipt_id__in=ids).order_by("used_on", "id"):
        offsets.setdefault(o.receipt_id, []).append(o)
    given: dict = {}
    for rid, day, amount in CashEntry.objects.filter(
        receipt_id__in=ids, article=CashEntry.Article.CHANGE, kind=CashEntry.Kind.OUT,
    ).values_list("receipt_id", "happened_on", "amount"):
        given.setdefault(rid, []).append((day, amount))

    held: dict = {}
    for rid, kind, amount in CashEntry.objects.filter(receipt_id__in=ids).values_list(
        "receipt_id", "kind", "amount"
    ):
        held[rid] = held.get(rid, ZERO) + (amount if kind == CashEntry.Kind.IN else -amount)

    rows: list[dict] = []
    metas = []
    for r in receipts:
        _part, meta = _receipt_rows(
            r, offsets.get(r.pk, []), sorted(given.get(r.pk, [])), held.get(r.pk)
        )
        metas.append(meta)
    _return_moved_change(metas)
    for part in (meta["rows"] for meta in metas):
        rows += part

    # Входящий долг на дату переезда из Excel (волна 2): дебет днём переезда,
    # его оплаты и списания — кредит своими днями. Отменённый — как не было.
    from .models import OpeningBalance

    for ob in OpeningBalance.objects.filter(
        client=client, kind=OpeningBalance.Kind.DEBT, reverted_at__isnull=True,
    ).prefetch_related("payments"):
        rows.append(_row(ob.as_of, "opening_debt", debit=ob.amount, note=ob.note, opening_id=ob.pk))
        for p in ob.payments.all():
            kind = "opening_write_off" if p.method == "WRITE_OFF" else "opening_payment"
            rows.append(_row(
                p.paid_on, kind, credit=p.amount, method=p.method,
                method_display=p.get_method_display(), note=p.note, opening_id=ob.pk,
            ))
            if p.cancelled_at:
                # Отмена оплаты (D-141): дебет днём отмены. Сама оплата остаётся
                # своим днём — акт прошлого месяца не переписывается.
                rows.append(_row(
                    _day(p.cancelled_at), f"{kind}_cancelled", debit=p.amount, method=p.method,
                    method_display=p.get_method_display(), note=p.cancel_reason, opening_id=ob.pk,
                ))

    for adv in ClientAdvance.objects.filter(client=client).prefetch_related("uses"):
        if adv.is_opening and adv.reverted_at:
            continue          # ошибочный входящий аванс отменён — его как не было
        rows.append(_row(
            adv.paid_on, "opening_advance" if adv.is_opening else "advance",
            credit=adv.amount, method=adv.method,
            method_display=adv.get_method_display(), note=adv.note, advance_id=adv.pk,
        ))
        used = sum((u.amount for u in adv.uses.all()), ZERO)
        if adv.reverted_at:
            rows.append(_row(_day(adv.reverted_at), "advance_reverted", debit=adv.amount, advance_id=adv.pk))
        else:
            gap = (adv.amount - adv.remaining) - used
            if gap:
                # Зачёт без записи (старый вызов): разницу — днём аванса.
                rows.append(_row(
                    adv.paid_on, "adjustment",
                    debit=gap if gap > 0 else ZERO, credit=-gap if gap < 0 else ZERO,
                    advance_id=adv.pk,
                ))
    rows = [r for r in rows if r["debit"] or r["credit"] or r["kind"] in ("order", "advance_used")]
    rows.sort(key=lambda x: (x["date"], x["order_number"] or 0, RANK.get(x["kind"], 99)))
    return rows


def build_statement(client, date_from: date | None = None, date_to: date | None = None) -> dict:
    """Выписка за период: входящее сальдо, строки, обороты, исходящее сальдо."""
    rows = statement_rows(client)
    opening = ZERO
    inside = []
    for row in rows:
        if date_from and row["date"] < date_from:
            opening += row["debit"] - row["credit"]
        elif date_to and row["date"] > date_to:
            continue
        else:
            inside.append(row)

    debit = sum((r["debit"] for r in inside), ZERO)
    credit = sum((r["credit"] for r in inside), ZERO)
    running = opening
    for row in inside:
        running += row["debit"] - row["credit"]
        row["balance"] = running
    closing = opening + debit - credit

    return {
        "client": {
            "id": client.pk, "display_name": client.display_name, "type": client.type,
            "phone": client.phone, "inn": client.inn,
        },
        "period": {"from": date_from, "to": date_to},
        "opening": opening,
        "opening_side": side_of(opening),
        "rows": inside,
        "turnover": {"debit": debit, "credit": credit},
        "closing": closing,
        "closing_side": side_of(closing),
        "closing_label": LABELS[side_of(closing)],
    }


def _plain(value):
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def statement_payload(client, date_from=None, date_to=None) -> dict:
    """Выписка в виде, готовом для JSON: деньги — строками «1234.50», даты — ISO."""
    return _plain(build_statement(client, date_from, date_to))
