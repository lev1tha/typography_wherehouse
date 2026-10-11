"""Входящие остатки клиентов при переезде из Excel (XL-04/F6/CLI-06/G3-N1, волна 2).

Что это. В день переезда у цеха в Excel висят долги клиентов и их авансы. Заказов
под ними в системе нет — и заводить фиктивные заказы нельзя: это выручка,
налог и прибыль, которые уже посчитаны в Excel. Поэтому:

- ДОЛГ на начало — `OpeningBalance(kind=DEBT)`: не выручка, не налог, не прибыль,
  не касса. Входит в долг и сальдо клиента (`opening_debt`), акт сверки
  (входящее сальдо), возраст долга (дата — дата переезда), список должников и
  плитку «Долг». Оплата (`pay_opening_debts`) — приход в кассу без чека;
  общая выплата клиента (`pay-debt`) гасит сначала его, потом заказы.
- АВАНС на начало — `ClientAdvance(is_opening=True)` без кассовой записи;
  тратится, как любой аванс.

Сверка ОПиУ → ОДДС: входящий долг и деньги, принесённые в его оплату, а также
траты входящего аванса — строкой «Входящие остатки» (`bridge_flows`); в уровни
«Долг клиентов» и «Сдача и предоплаты» сверки они не входят.

Вставка из Excel (`parse_text`): строка — «телефон · имя · сумма» (плюс — долг,
минус — аванс) или «телефон · имя · долг · аванс»; разделитель — табуляция
(как копирует Excel) или «;». В числах — пробелы и запятая: «12 000,50».
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import Client, ClientAdvance, OpeningBalance, OpeningDebtPayment

ZERO = Decimal("0")
CENT = Decimal("0.01")
MAX_ROWS = 2000


class OpeningRejected(Exception):
    """Отказ: текст уходит пользователю как есть."""


# --- Долг клиента: входящая часть -----------------------------------------------------


def open_debts_qs():
    """Неоплаченные входящие долги (не отменённые)."""
    return OpeningBalance.objects.filter(
        kind=OpeningBalance.Kind.DEBT, reverted_at__isnull=True, remaining__gt=0,
    )


def opening_debt(client) -> Decimal:
    """Сколько клиент ещё должен по входящему долгу (часть его долга)."""
    if client is None or client.pk is None:
        return ZERO
    return open_debts_qs().filter(client=client).aggregate(v=Sum("remaining"))["v"] or ZERO


def opening_oldest(client):
    """Момент самого старого неоплаченного входящего долга или None."""
    if client is None or client.pk is None:
        return None
    first = open_debts_qs().filter(client=client).order_by("as_of_at").first()
    return first.as_of_at if first else None


# --- Разбор вставки -------------------------------------------------------------------

_SPACES = re.compile(r"[\s    ']+")


def parse_number(raw) -> Decimal | None:
    """«12 000,50» → 12000.50; пусто → None. Кривое — ValueError с причиной.

    Запятая — десятичная (так пишет Excel с русской локалью); если в числе и
    точка, и запятая, десятичный — последний из знаков. Несколько запятых без
    точки — разделители тысяч («1,234,567»). Больше двух знаков после запятой —
    отказ: деньги в сомах с тыйынами.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.sub(r"(?i)(сом|сомов|som|kgs|с\.?)$", "", text).strip()
    text = _SPACES.sub("", text)
    text = text.replace("−", "-").replace("–", "-")
    if text in ("", "-", "0", "-0"):
        return ZERO if text.lstrip("-") == "0" else None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif text.count(",") > 1:
        text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        raise ValueError(f"«{raw}» — не число")
    # Телефон в колонке суммы (RM-N9, D-186): 12+ цифр подряд без разделителей
    # («996700100200») или номер с ведущим нулём («0555112233») — не долг на
    # сотни миллиардов, а ошибка строки.
    digits = text.lstrip("-")
    plain = re.sub(r"(?i)(сом|сомов|som|kgs|с\.?)$", "", str(raw or "").strip()).strip().lstrip("-−–")
    if digits.isdigit() and (
        (len(digits) >= 12 and plain.isdigit()) or (digits.startswith("0") and len(digits) >= 9)
    ):
        raise ValueError(f"«{raw}» — похоже на телефон, а не на сумму")
    value = Decimal(text)
    if value != value.quantize(CENT, rounding=ROUND_HALF_UP):
        raise ValueError(f"«{raw}» — больше двух знаков после запятой")
    from .amounts import MAX_AMOUNT

    if abs(value) >= MAX_AMOUNT:
        raise ValueError(f"«{raw}» — слишком большая сумма (должна быть меньше 10 000 000 000)")
    return value.quantize(CENT)


def _split(line: str) -> list[str]:
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if ";" in line:
        return [c.strip() for c in line.split(";")]
    return [line.strip()]


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def parse_text(text: str) -> list[dict]:
    """Строки вставки: {line, phone, name, debt, advance, errors}."""
    rows = []
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        cells = _split(raw)
        while cells and not cells[-1]:
            cells.pop()
        if len(cells) < 3:
            rows.append({
                "line": number, "phone": cells[0] if cells else "", "name": "",
                "debt": None, "advance": None,
                "errors": ["Нужно не меньше трёх колонок: телефон, имя, сумма."],
            })
            continue
        phone, name = cells[0], cells[1]
        # Имя и телефон перепутали местами — узнаём телефон по цифрам.
        if len(_digits(phone)) < 7 <= len(_digits(name)):
            phone, name = name, phone
        errors = []
        debt = advance = None
        try:
            if len(cells) >= 4:
                debt = parse_number(cells[2])
                advance = parse_number(cells[3])
                if (debt or ZERO) < 0 or (advance or ZERO) < 0:
                    errors.append("В колонках «долг» и «аванс» суммы без минуса.")
            else:
                value = parse_number(cells[2])
                if value is not None and value < 0:
                    advance = -value
                else:
                    debt = value
        except ValueError as e:
            errors.append(str(e))
        rows.append({
            "line": number, "phone": phone, "name": name.strip()[:255],
            "debt": debt, "advance": advance, "errors": errors,
        })
    # Первая строка — заголовок («Телефон · Имя · Долг»): в ней нет ни одной цифры
    # телефона и ни одной суммы.
    if rows and not _digits(rows[0]["phone"]) and rows[0]["errors"]:
        rows = rows[1:]
    if len(rows) > MAX_ROWS:
        raise OpeningRejected(f"За раз — не больше {MAX_ROWS} строк.")
    return rows


# --- Предпросмотр ----------------------------------------------------------------------


def _same_name(a: str, b: str) -> bool:
    norm = lambda v: re.sub(r"[^\wа-яё]+", " ", (v or "").lower()).strip()  # noqa: E731
    return norm(a) == norm(b)


def preview(rows: list[dict]) -> dict:
    """Что будет сделано по каждой строке: найден клиент / будет создан / ошибка."""
    from .phones import find_client_by_phone, phone_key
    from .serializers import MAX_PHONE_DIGITS, MIN_PHONE_DIGITS

    clients = list(Client.objects.all())
    existing = {
        (cid, kind) for cid, kind in OpeningBalance.objects.filter(reverted_at__isnull=True)
        .values_list("client_id", "kind")
    }
    seen: dict = {}
    out = []
    totals = {"debt": ZERO, "advance": ZERO, "create": 0, "found": 0, "errors": 0, "rows": 0}
    for row in rows:
        errors = list(row["errors"])
        warnings = []
        digits = _digits(row["phone"])
        client = None
        if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
            errors.append(f"Телефон: от {MIN_PHONE_DIGITS} до {MAX_PHONE_DIGITS} цифр.")
        else:
            client = find_client_by_phone(row["phone"], clients)
            key = phone_key(row["phone"])
            if key in seen:
                errors.append(f"Этот телефон уже есть в строке {seen[key]}.")
            else:
                seen[key] = row["line"]
        debt, advance = row["debt"] or ZERO, row["advance"] or ZERO
        if not errors and debt <= 0 and advance <= 0:
            errors.append("Сумма не указана или ноль.")
        if debt > 0 and advance > 0:
            warnings.append("debt_and_advance")
        if client is not None:
            for kind, value in ((OpeningBalance.Kind.DEBT, debt), (OpeningBalance.Kind.ADVANCE, advance)):
                if value > 0 and (client.pk, kind) in existing:
                    errors.append(
                        "У клиента уже есть входящий "
                        + ("долг" if kind == OpeningBalance.Kind.DEBT else "аванс")
                        + " — отмените прежний, если его надо заменить."
                    )
            if row["name"] and not _same_name(row["name"], client.display_name):
                warnings.append("name_differs")
        elif not errors and not row["name"]:
            warnings.append("no_name")
        status = "error" if errors else ("found" if client is not None else "create")
        totals["rows"] += 1
        if status == "error":
            totals["errors"] += 1
        else:
            totals["debt"] += debt
            totals["advance"] += advance
            totals["found" if client is not None else "create"] += 1
        out.append({
            "line": row["line"], "phone": row["phone"], "name": row["name"],
            "debt": debt, "advance": advance, "status": status,
            "client": (
                {"id": client.pk, "display_name": client.display_name, "phone": client.phone}
                if client is not None else None
            ),
            "errors": errors, "warnings": warnings,
        })
    return {"rows": out, "totals": totals}


# --- Проведение ------------------------------------------------------------------------


def batch_for_key(record) -> str:
    """Имя партии для проведения с ключом повтора (CLI-14): из записи ключа, так
    что повтор находит ту же партию, а ключ, занятый заново через неделю, —
    уже другую."""
    raw = f"{record.user_id}:{record.key}:{record.pk}".encode()
    return "k" + hashlib.sha1(raw).hexdigest()[:11]


@transaction.atomic
def post(text: str, as_of: date, *, user=None, note: str = "", batch: str | None = None) -> dict:
    """Провести вставку целиком: всё или ничего (строка с ошибкой — отказ).
    `batch` — имя партии (по умолчанию случайное)."""
    if as_of is None:
        raise OpeningRejected("Укажите дату переезда.")
    if as_of > timezone.localdate():
        raise OpeningRejected("Дата переезда не может быть в будущем.")
    # Дата остатка — день его входа в долг, акт сверки и возраст долга: в
    # закрытом месяце он переписал бы принятые цифры (RM-N8, D-186).
    from finance.periods import PeriodClosed, ensure_open

    try:
        ensure_open(as_of, "Провести входящие остатки этой датой")
    except PeriodClosed as e:
        raise OpeningRejected(" ".join(str(x) for x in (e.detail if isinstance(e.detail, list) else [e.detail])))
    plan = preview(parse_text(text))
    bad = [r for r in plan["rows"] if r["status"] == "error"]
    if not plan["rows"]:
        raise OpeningRejected("Нечего проводить: вставьте строки из Excel.")
    if bad:
        raise OpeningRejected(
            "Исправьте строки с ошибками: " + ", ".join(str(r["line"]) for r in bad[:20])
            + ("…" if len(bad) > 20 else "") + "."
        )
    batch = batch or uuid.uuid4().hex[:12]
    note = (note or "").strip()[:255]
    created_clients = 0
    made = []
    for row in plan["rows"]:
        if row["client"] is not None:
            client = Client.objects.get(pk=row["client"]["id"])
        else:
            client = Client.objects.create(
                type=Client.Type.PHYSICAL, full_name=row["name"] or row["phone"],
                phone=row["phone"].strip()[:32],
            )
            created_clients += 1
        if row["debt"] > 0:
            made.append(OpeningBalance.objects.create(
                client=client, kind=OpeningBalance.Kind.DEBT, amount=row["debt"],
                remaining=row["debt"], as_of=as_of, note=note, batch=batch, created_by=user,
            ))
        if row["advance"] > 0:
            advance = ClientAdvance.objects.create(
                client=client, amount=row["advance"], remaining=row["advance"],
                method=ClientAdvance.Method.CASH, paid_on=as_of, is_opening=True,
                note=(note or "Входящий остаток при переезде")[:255], created_by=user,
            )
            made.append(OpeningBalance.objects.create(
                client=client, kind=OpeningBalance.Kind.ADVANCE, amount=row["advance"],
                remaining=ZERO, as_of=as_of, advance=advance, note=note, batch=batch,
                created_by=user,
            ))
    return {
        "batch": batch, "created_clients": created_clients, "balances": made,
        "debt": plan["totals"]["debt"], "advance": plan["totals"]["advance"],
    }


@transaction.atomic
def revert(balance: OpeningBalance, *, user=None) -> OpeningBalance:
    """Отменить ошибочный входящий остаток — пока по нему ничего не оплачено и из
    аванса ничего не зачтено. Касса не двигается (её и не было)."""
    balance = OpeningBalance.objects.select_for_update().get(pk=balance.pk)
    if balance.reverted_at:
        raise OpeningRejected("Этот остаток уже отменён.")
    if balance.kind == OpeningBalance.Kind.DEBT:
        # Отменённые оплаты (D-141) не мешают: деньги по ним вернулись встречной
        # записью, остаток снова целый.
        active = balance.payments.filter(cancelled_at__isnull=True)
        if active.exists() or balance.remaining != balance.amount:
            raise OpeningRejected("По этому долгу уже есть оплаты — отменить его целиком нельзя.")
        balance.remaining = ZERO
    else:
        adv = ClientAdvance.objects.select_for_update().get(pk=balance.advance_id)
        if adv.remaining != adv.amount or adv.reverted_at:
            raise OpeningRejected("Из этого аванса уже зачтено в заказы — отменить его целиком нельзя.")
        adv.reverted_at = timezone.now()
        adv.remaining = ZERO
        adv.save(update_fields=["reverted_at", "remaining"])
    balance.reverted_at = timezone.now()
    balance.save(update_fields=["reverted_at", "remaining"])
    return balance


# --- Оплата входящего долга ---------------------------------------------------------------


@transaction.atomic
def pay_opening_debts(client, amount=None, *, ids=None, user=None, paid_on=None, method=None, note=""):
    """Погасить входящие долги клиента (старые первыми). Возвращает
    `(по остаткам [(остаток, сумма)], не пригодилось)`; `amount=None` — всё.

    Деньги — приход в кассу по статье «Оплата от клиента» без чека; списание
    (`WRITE_OFF`, только админ) — расход «Безнадёжные долги» без кассы.
    """
    from finance import cash
    from finance.models import CashEntry, ExpenseEntry

    method = str(method or "CASH").upper()
    if method == "ONLINE":
        method = "CASH"
    if method not in OpeningDebtPayment.Method.values:
        raise OpeningRejected("Способ оплаты: " + ", ".join(OpeningDebtPayment.Method.values) + ".")
    writing_off = method == OpeningDebtPayment.Method.WRITE_OFF
    if writing_off and not getattr(user, "is_admin_role", False):
        raise OpeningRejected("Списать долг может только администратор.")
    day = paid_on or timezone.localdate()
    left = None if amount is None else Decimal(str(amount))
    qs = open_debts_qs().select_for_update().filter(client=client).order_by("as_of", "id")
    if ids is not None:
        qs = qs.filter(pk__in=list(ids))
    allocations = []
    for ob in qs:
        if left is not None and left <= 0:
            break
        take = ob.remaining if left is None else min(left, ob.remaining)
        if take <= 0:
            continue
        payment = OpeningDebtPayment(
            opening=ob, amount=take, method=method, paid_on=day,
            note=(note or "")[:255], created_by=user,
        )
        if writing_off:
            from sales.sale_service import bad_debt_kind

            expense = ExpenseEntry.objects.create(
                kind=bad_debt_kind(), amount=take, spent_at=day, created_by=user,
                name=f"Списание входящего долга «{client.display_name}»", note=note or "",
            )
            payment.expense_id = expense.pk
        else:
            entry = cash.money_in(
                take, CashEntry.Article.SALE, payment_method=method, happened_on=day, user=user,
                note=f"Оплата входящего долга клиента «{client.display_name}»",
            )
            payment.cash_entry_id = entry.pk if entry else None
        payment.save()
        ob.remaining -= take
        ob.save(update_fields=["remaining"])
        allocations.append((ob, take))
        if left is not None:
            left -= take
    return allocations, (left if left is not None else ZERO)


@transaction.atomic
def cancel_opening_payment(balance: OpeningBalance, payment_id, *, user=None, reason="") -> OpeningDebtPayment:
    """Отменить ОДНУ оплату входящего долга (D-141, волна 3) — по образцу
    отмены оплаты заказа (`sales.sale_service.cancel_payment`).

    Деньги: исходный приход остаётся в кассовой книге, а сегодняшним днём
    пишется встречный расход («Откат оплаты») с того же счёта. Запись оплаты
    не удаляется, а помечается отменённой: акт сверки за прошлый период её
    помнит, отмена ложится своей строкой своего дня — сальдо сходится с долгом
    карточки и на сегодня, и на любую прошлую дату.

    Списание (`WRITE_OFF`) кассы не трогало — отмена убирает его расход
    «Безнадёжные долги». Расход лежит в месяце списания, поэтому этот месяц
    должен быть открыт (иначе поменялась бы прибыль закрытого месяца).

    Остаток долга растёт на сумму оплаты. Замок периода по сегодняшней дате
    (встречная запись) ставит вьюха, как у отмены оплаты заказа.
    """
    from finance import cash
    from finance.models import CashEntry, ExpenseEntry
    from finance.periods import ensure_open

    balance = OpeningBalance.objects.select_for_update().get(pk=balance.pk)
    if balance.kind != OpeningBalance.Kind.DEBT:
        raise OpeningRejected("Оплаты бывают только у входящего долга.")
    if balance.reverted_at:
        raise OpeningRejected("Этот остаток отменён — его оплаты уже не про долг клиента.")
    try:
        payment = OpeningDebtPayment.objects.select_for_update().get(pk=int(payment_id), opening=balance)
    except (OpeningDebtPayment.DoesNotExist, TypeError, ValueError):
        raise OpeningRejected("Оплата не найдена у этого входящего долга.")
    if payment.cancelled_at:
        raise OpeningRejected("Эта оплата уже отменена.")
    if balance.remaining + payment.amount > balance.amount:
        # Данные разошлись (остаток правили руками): вернуть больше долга нельзя.
        raise OpeningRejected("Остаток долга не сходится с его оплатами — отмена невозможна.")
    reason = (reason or "").strip()[:200]
    who = balance.client.display_name
    if payment.method == OpeningDebtPayment.Method.WRITE_OFF:
        ensure_open(payment.paid_on, "Отменить списание долга этого месяца")
        if payment.expense_id:
            ExpenseEntry.objects.filter(pk=payment.expense_id).delete()
    else:
        account = (
            CashEntry.objects.filter(pk=payment.cash_entry_id).values_list("account", flat=True).first()
            if payment.cash_entry_id else None
        )
        label = f"Отмена оплаты входящего долга клиента «{who}»"
        entry = cash.money_out(
            payment.amount, CashEntry.Article.UNPAY,
            account=account or cash.account_for(payment.method), user=user,
            note=f"{label}: {reason}" if reason else label,
        )
        payment.cancel_cash_entry_id = entry.pk if entry else None
    payment.cancelled_at = timezone.now()
    payment.cancelled_by = user if getattr(user, "pk", None) else None
    payment.cancel_reason = reason
    payment.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason", "cancel_cash_entry_id"])
    balance.remaining += payment.amount
    balance.save(update_fields=["remaining"])
    return payment


# --- Сверка ОПиУ → ОДДС -----------------------------------------------------------------


def opening_cash_entry_ids() -> set:
    """Записи кассы — оплаты входящего долга и встречные записи их отмен (D-141):
    их нет в долгах и деньгах клиентов, они идут строкой «Входящие остатки»."""
    ids = set()
    for paid, back in OpeningDebtPayment.objects.filter(
        Q(cash_entry_id__isnull=False) | Q(cancel_cash_entry_id__isnull=False),
    ).values_list("cash_entry_id", "cancel_cash_entry_id"):
        ids.update(pk for pk in (paid, back) if pk)
    return ids


def opening_advance_uses():
    """[(клиент, день, сумма)] — траты входящих авансов в заказы."""
    from .models import BalanceOffset

    return list(
        BalanceOffset.objects.filter(
            source=BalanceOffset.Source.ADVANCE, advance__is_opening=True,
        ).values_list("client_id", "used_on", "amount")
    )


def balances_payload(qs) -> list[dict]:
    rows = []
    for b in qs.select_related("client", "advance").prefetch_related(
        "payments__created_by", "payments__cancelled_by",
    ):
        live = [p for p in b.payments.all() if not p.cancelled_at]
        paid = sum((p.amount for p in live if p.method != "WRITE_OFF"), ZERO)
        written_off = sum((p.amount for p in live if p.method == "WRITE_OFF"), ZERO)
        rows.append({
            "id": b.id, "kind": b.kind, "amount": b.amount,
            "remaining": b.advance.remaining if b.advance_id else b.remaining,
            "paid": paid, "written_off": written_off,
            # Оплаты по одной — чтобы админ мог отменить ошибочную (D-141).
            "payments": [
                {
                    "id": p.id, "amount": p.amount, "method": p.method,
                    "method_display": p.get_method_display(), "paid_on": p.paid_on,
                    "note": p.note,
                    "created_by_name": p.created_by.username if p.created_by_id else None,
                    "cancelled": bool(p.cancelled_at),
                    "cancelled_at": p.cancelled_at,
                    "cancel_reason": p.cancel_reason,
                    "cancelled_by_name": p.cancelled_by.username if p.cancelled_by_id else None,
                }
                for p in b.payments.all()
            ],
            "as_of": b.as_of, "note": b.note, "batch": b.batch,
            "client": {"id": b.client_id, "display_name": b.client.display_name, "phone": b.client.phone},
            "reverted": bool(b.reverted_at),
            "created_at": b.created_at,
        })
    return rows
