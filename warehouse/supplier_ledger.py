"""Расчёты с поставщиками: платежи строками, аванс и зачёт, сальдо, валюта.

Аудит «владелец против Excel» 10.10 (cash-06, cash-07, G1-N3, F1):

* платёж поставщику — отдельная строка (`SupplierPayment`) со своей датой,
  суммой, счётом и автором; по накладной или «авансом без накладной»;
* сальдо поставщика = начальный долг + накладные − платежи; аванс и
  переплата видны (минус — это деньги, лежащие у поставщика);
* накладная в валюте: платёж идёт в валюте накладной по курсу на день
  оплаты, а разница с курсом накладной — расход (доход) вида «Курсовая
  разница» (`FX_DIFF`);
* карточка поставщика: накладные, платежи, возвраты, сальдо — и CSV для Excel.

Деньги — только `Decimal`. Кассу пишут функции `finance.cash` (книга не
подчищается: правка и удаление платежа — встречные записи).
"""
from __future__ import annotations

import csv
import io
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .models import (
    Supplier,
    SupplierOpeningDebt,
    SupplierPayment,
    SupplierReturn,
    Supply,
)
from .supplies import SupplyError

CENT = Decimal("0.01")
ZERO = Decimal("0")
TINY = Decimal("0.005")


def q2(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _dec(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — любой мусор в числе = «некорректная сумма»
        raise SupplyError("Некорректная сумма.")


def _label(supply: Supply) -> str:
    return supply.number or f"#{supply.pk}"


def _n(value) -> str:
    """Число для описания: без хвоста нулей, с запятой — «87,45», «150»."""
    text = format(Decimal(value).normalize(), "f")
    return text.replace(".", ",")


# Курс оплаты дальше этой доли от курса накладной — похоже на опечатку
# (RU-N18): «881» вместо «88,1» списывал из кассы в десять раз больше молча.
RATE_TOLERANCE = Decimal("0.10")


class RateNeedsConfirmation(SupplyError):
    """Курс оплаты далеко от курса накладной — нужен явный «да» (409)."""

    code = "rate_far"


def _check_rate(supply: Supply, used_rate: Decimal) -> None:
    base = supply.rate or ZERO
    if base <= 0:
        return
    if abs(used_rate - base) > base * RATE_TOLERANCE:
        pct = ((used_rate - base) / base * 100).quantize(Decimal("1"))
        raise RateNeedsConfirmation(
            f"Курс оплаты {_n(used_rate)} отличается от курса накладной {_n(base)} на "
            f"{'+' if pct > 0 else ''}{pct} % — это не опечатка? Курсовая разница уйдёт в расходы "
            "или доходы. Проверьте курс или подтвердите."
        )


# --- Курсовая разница -----------------------------------------------------------


def fx_kind():
    """Вид расхода «Курсовая разница». Его заводит миграция финансов; если её
    нет (старая база, тест) — создаём тот же встроенный вид."""
    from finance.models import ExpenseKind

    kind = ExpenseKind.objects.filter(code="FX_DIFF").first()
    if kind is None:
        kind, _created = ExpenseKind.objects.get_or_create(
            code="FX_DIFF",
            defaults={
                "name": "Курсовая разница", "block": ExpenseKind.Block.VARIABLE,
                "role": ExpenseKind.Role.OPEX, "is_builtin": True, "position": 50,
            },
        )
    return kind


def _book_fx(fx: Decimal, account: str, day, note: str, user):
    """Курсовая разница в учёт. Плюс — расход, деньги ушли с того же счёта
    (запись «Расход цеха»); минус — доход, денег не двигает (платили меньше, чем
    закрыли долга). Возвращает трату или None."""
    from finance import cash
    from finance.models import ExpenseEntry

    if not fx:
        return None
    entry = ExpenseEntry.objects.create(
        kind=fx_kind(), account=account, name=note[:255], amount=fx, spent_at=day,
        created_by=user,
    )
    if fx > 0:
        cash.sync_expense(entry, user=user)
    return entry


def reverse_fx(payment: SupplierPayment, note: str, user=None) -> None:
    """Отмена платежа: курсовая разница уходит встречной тратой сегодняшним днём
    (прошлый месяц остаётся как был). Расход возвращает деньги в кассу."""
    from finance import cash
    from finance.models import CashEntry, ExpenseEntry

    fx = payment.fx_diff or ZERO
    if not fx:
        return
    today = timezone.localdate()
    counter = ExpenseEntry.objects.create(
        kind=fx_kind(), account=payment.account, name=note[:255], amount=-fx,
        spent_at=today, created_by=user,
    )
    if fx > 0:
        cash.money_in(fx, CashEntry.Article.EXPENSE, account=payment.account,
                      expense=counter, note=note, user=user)


# --- Платежи ----------------------------------------------------------------------


def _check_account(account):
    if account not in ("CASH", "BANK"):
        raise SupplyError("Укажите, чем платили: наличными или с банка.")


@transaction.atomic
def record_payment(*, supply=None, supplier=None, amount, account, paid_on=None,
                   rate=None, note="", user=None, confirm_rate=False) -> SupplierPayment:
    """Заплатить поставщику: по накладной либо авансом (supply не указана).

    ``amount`` — в сомах; у накладной в валюте — В ВАЛЮТЕ накладной, и тогда
    обязателен ``rate`` (сом за единицу валюты на день оплаты). Курс дальше
    10 % от курса накладной — `RateNeedsConfirmation`, пока не передан
    ``confirm_rate`` (RU-N18).

    Деньги в валюте (RU-N17): курсовая разница — ЦЕЛЫМИ сомами, «закрыто
    долга» — остальное. Тогда «Оплата поставщику» + «Курсовая разница» в кассе
    на экране (до сома) дают ровно «С кассы уйдёт …» из окна оплаты; раньше
    13 117,50 + 97,50 показывались как 13 118 + 98 = 13 216 при 13 215 в окне.
    Платёж, закрывающий долг, закрывает его до тыйына (долг в сомах), а сумма
    по кассе — долг + целая разница: отличается от валюта × курс меньше чем
    на полсома.
    """
    from finance import cash
    from finance.periods import ensure_open

    value = _dec(amount)
    if value is None or value <= 0:
        raise SupplyError("Сумма должна быть больше нуля.")
    _check_account(account)
    paid_on = paid_on or timezone.localdate()
    ensure_open(paid_on, "Провести оплату этой датой")

    currency, amount_fc, used_rate = "KGS", None, None
    fx = ZERO
    if supply is not None:
        supply = Supply.objects.select_for_update().get(pk=supply.pk)
        if supply.is_opening:
            raise SupplyError("Накладная начальных остатков — платить по ней нечего.")
        supplier = supply.supplier
        debt = supply.debt
        if supply.is_foreign:
            used_rate = _dec(rate)
            if used_rate is None or used_rate <= 0:
                raise SupplyError(
                    f"Накладная в {supply.currency}: укажите курс на день оплаты "
                    "(сколько сом за единицу валюты)."
                )
            debt_fc = supply.debt_foreign or ZERO
            amount_fc = q2(value)
            if amount_fc > debt_fc + TINY:
                raise SupplyError(
                    f"По накладной долг {debt_fc} {supply.currency} — больше заплатить нельзя."
                )
            if not confirm_rate:
                _check_rate(supply, used_rate)
            currency = supply.currency
            cash_amount = q2(amount_fc * used_rate)
            closing = amount_fc >= debt_fc - TINY
            # Закрываем остаток целиком — берём весь долг в сомах, без копеечных хвостов.
            exact = debt if closing else q2(amount_fc * supply.rate)
            exact = min(exact, debt)
            fx = (cash_amount - exact).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if closing:
                settled = exact
                cash_amount = settled + fx
            else:
                settled = cash_amount - fx
                if settled > debt:
                    settled = debt
                    fx = cash_amount - settled
        else:
            if value > debt:
                raise SupplyError(f"По накладной долг {debt} — больше заплатить нельзя.")
            cash_amount = settled = q2(value)
    else:
        if supplier is None:
            raise SupplyError("Аванс записывается на поставщика — выберите его.")
        cash_amount = settled = q2(value)

    head = (
        f"Оплата накладной {_label(supply)}" if supply is not None
        else f"Аванс поставщику {supplier.name}"
    )
    text = f"{head}. {note}".strip(". ") if note else head
    entry = cash.supplier_paid(
        settled if fx > 0 else cash_amount, account, supply=supply,
        happened_on=paid_on, note=text[:255], user=user,
    )
    # Без «Курсовая разница:» впереди — касса сама ставит вид расхода перед
    # названием, и строка выходила «Курсовая разница: Курсовая разница: …»;
    # курсы — без хвоста нулей («87,45», а не «87.450000»).
    expense = _book_fx(
        fx, account, paid_on,
        f"{head}: {_n(amount_fc)} {currency} по {_n(used_rate)} вместо {_n(supply.rate)}",
        user,
    ) if fx else None
    return SupplierPayment.objects.create(
        supplier=supplier, supply=supply, kind=SupplierPayment.Kind.PAYMENT,
        paid_on=paid_on, account=account, amount=cash_amount, fx_diff=fx,
        currency=currency, amount_fc=amount_fc, rate=used_rate, note=note[:255],
        created_by=user, cash_entry=entry, fx_expense=expense,
    )


def advance_remaining(payment: SupplierPayment) -> Decimal:
    """Сколько аванса ещё не зачтено в накладные."""
    used = sum((o.amount for o in payment.offsets.all()), ZERO)
    return payment.amount - used


@transaction.atomic
def apply_advance(advance: SupplierPayment, supply: Supply, amount, *, paid_on=None,
                  user=None) -> SupplierPayment:
    """Зачесть часть аванса в накладную: деньги не двигаются, долг накладной
    уменьшается, остаток аванса тоже."""
    advance = SupplierPayment.objects.select_for_update().get(pk=advance.pk)
    supply = Supply.objects.select_for_update().get(pk=supply.pk)
    if not advance.is_advance:
        raise SupplyError("Зачесть можно только аванс — платёж без накладной.")
    if supply.is_opening:
        raise SupplyError("Накладная начальных остатков — зачитывать нечего.")
    if advance.supplier_id is None or advance.supplier_id != supply.supplier_id:
        raise SupplyError("Аванс и накладная должны быть одного поставщика.")
    value = _dec(amount)
    if value is None or value <= 0:
        raise SupplyError("Сумма должна быть больше нуля.")
    value = q2(value)
    left = advance_remaining(advance)
    if value > left:
        raise SupplyError(f"В авансе осталось {left} — больше зачесть нельзя.")
    if value > supply.debt:
        raise SupplyError(f"По накладной долг {supply.debt} — больше зачесть нельзя.")
    return SupplierPayment.objects.create(
        supplier=advance.supplier, supply=supply, source=advance,
        kind=SupplierPayment.Kind.OFFSET, paid_on=paid_on or timezone.localdate(),
        amount=value, currency="KGS",
        note=f"Зачёт аванса от {advance.paid_on:%d.%m.%Y}", created_by=user,
    )


def _cash_part(p: SupplierPayment) -> Decimal:
    """Сколько в кассовой записи «Оплата поставщику» по этой строке."""
    if p.kind == SupplierPayment.Kind.REFUND:
        return p.amount
    return p.settled if (p.fx_diff or ZERO) > 0 else p.amount


@transaction.atomic
def update_payment(payment: SupplierPayment, *, account=None, paid_on=None, note=None,
                   user=None) -> list[str]:
    """Править платёж: счёт, дату, примечание. Двигается только его сумма.

    Счёт меняется встречной записью на старом счёте и новой записью на новом —
    книга не подчищается. Платёж с курсовой разницей кроме примечания не
    правится: удалите и внесите заново.
    """
    from finance import cash
    from finance.models import CashEntry
    from finance.periods import ensure_open

    p = SupplierPayment.objects.select_for_update().get(pk=payment.pk)
    changes: list[str] = []
    new_account = account if account not in (None, "") else p.account
    new_day = paid_on or p.paid_on
    money_changes = (new_account != p.account) or (new_day != p.paid_on)
    if money_changes:
        if p.kind == SupplierPayment.Kind.OFFSET:
            raise SupplyError("У зачёта аванса нет счёта и кассовой записи — правится только примечание.")
        if p.fx_diff or p.fx_expense_id or p.amount_fc is not None:
            raise SupplyError(
                "Платёж с курсовой разницей не правится: удалите его и внесите заново."
            )
        _check_account(new_account)
        if new_day > timezone.localdate():
            raise SupplyError("Дата платежа не может быть в будущем.")
        ensure_open(p.paid_on, "Править платёж закрытого периода")
        ensure_open(new_day, "Перенести платёж в закрытый период")
        entry = p.cash_entry
        if new_account != p.account:
            kind = entry.kind if entry is not None else (
                CashEntry.Kind.IN if p.kind == SupplierPayment.Kind.REFUND else CashEntry.Kind.OUT
            )
            opposite = CashEntry.Kind.IN if kind == CashEntry.Kind.OUT else CashEntry.Kind.OUT
            note_text = f"Правка платежа поставщику: счёт {p.get_account_display()} → " \
                        f"{dict(p._meta.get_field('account').choices)[new_account]}"
            cash.record(opposite, p.amount, CashEntry.Article.SUPPLY, account=p.account,
                        supply=p.supply, happened_on=p.paid_on, note=note_text, user=user)
            new_entry = cash.record(kind, p.amount, CashEntry.Article.SUPPLY, account=new_account,
                                    supply=p.supply, happened_on=new_day, note=note_text, user=user)
            p.cash_entry = new_entry
            changes.append(
                f"счёт {p.get_account_display()} → {dict(p._meta.get_field('account').choices)[new_account]}"
            )
            p.account = new_account
        elif entry is not None and new_day != p.paid_on:
            entry.happened_on = new_day
            entry.save(update_fields=["happened_on"])
        if new_day != p.paid_on:
            changes.append(f"дата {p.paid_on:%d.%m.%Y} → {new_day:%d.%m.%Y}")
            p.paid_on = new_day
    if note is not None and note != p.note:
        changes.append(f"примечание «{p.note}» → «{note}»")
        p.note = note[:255]
    if changes:
        p.save()
    return changes


@transaction.atomic
def delete_payment(payment: SupplierPayment, *, user=None) -> str:
    """Удалить платёж: деньги возвращаются в кассу встречной записью сегодняшним
    днём (исходная запись остаётся в книге). Возвращает текст для журнала."""
    from finance import cash
    from finance.models import CashEntry
    from finance.periods import ensure_open

    p = SupplierPayment.objects.select_for_update().get(pk=payment.pk)
    if p.is_advance and p.offsets.exists():
        raise SupplyError("Из этого аванса уже закрыты накладные — сначала уберите зачёты.")
    where = f"накладная {_label(p.supply)}" if p.supply_id else "аванс"
    text = f"{p.get_kind_display()} {p.amount} сом от {p.paid_on:%d.%m.%Y} ({where})"
    if p.kind != SupplierPayment.Kind.OFFSET:
        ensure_open(timezone.localdate(), "Отменить платёж поставщику сегодняшним днём")
        note = f"Отмена платежа поставщику от {p.paid_on:%d.%m.%Y}"
        part = _cash_part(p)
        if p.kind == SupplierPayment.Kind.REFUND:
            cash.money_out(part, CashEntry.Article.SUPPLY, account=p.account, supply=p.supply,
                           note=note, user=user)
        else:
            cash.money_in(part, CashEntry.Article.SUPPLY, account=p.account, supply=p.supply,
                          note=note, user=user)
            reverse_fx(p, note, user)
    p.delete()
    return text


# --- Начальный долг -----------------------------------------------------------------


@transaction.atomic
def add_opening_debt(supplier: Supplier, amount, *, as_of=None, note="", user=None) -> SupplierOpeningDebt:
    value = _dec(amount)
    if value is None or value == 0:
        raise SupplyError("Укажите сумму долга (минус — поставщик должен нам).")
    return SupplierOpeningDebt.objects.create(
        supplier=supplier, amount=q2(value), as_of=as_of or timezone.localdate(),
        note=note[:255], created_by=user,
    )


# --- Сальдо и карточка поставщика ------------------------------------------------------


def supplier_balance(supplier: Supplier) -> dict:
    """Сальдо поставщика по ПРЕДЗАГРУЖЕННЫМ связям (`supplies__lines`,
    `supplies__payments`, `payments`, `opening_debts`): плюс — мы должны,
    минус — деньги лежат у поставщика (аванс, переплата)."""
    supplies = list(supplier.supplies.all())
    real = [s for s in supplies if not s.is_opening]
    # Возврат датой возврата (накладная закрытого месяца не переписана) —
    # минус к накладным: как строка с минусом в Excel.
    invoices = sum((s.total_cost - s.returned_after for s in real), ZERO)
    legacy_paid = sum((s.paid_amount or ZERO for s in real), ZERO)
    payments = list(supplier.payments.all())
    paid_rows = sum((p.supplier_effect for p in payments), ZERO)
    opening = sum((d.amount for d in supplier.opening_debts.all()), ZERO)
    saldo = opening + invoices - legacy_paid - paid_rows
    advances = sum((advance_remaining(p) for p in payments if p.is_advance), ZERO)
    foreign: dict[str, Decimal] = {}
    for s in real:
        fd = s.debt_foreign
        if fd:
            foreign[s.currency] = foreign.get(s.currency, ZERO) + fd
    return {
        "opening": q2(opening),
        "invoices": q2(invoices),
        "paid": q2(legacy_paid + paid_rows),
        "saldo": q2(saldo),
        "owe": q2(saldo) if saldo > 0 else ZERO,
        "credit": q2(-saldo) if saldo < 0 else ZERO,
        "advances": q2(advances),
        "foreign_debts": {k: str(q2(v)) for k, v in foreign.items()},
    }


def statement(supplier: Supplier, d_from=None, d_to=None) -> dict:
    """Выписка по поставщику: строки в порядке дат, с остатком после каждой.

    ``delta`` — влияние на сальдо: плюс увеличивает наш долг (накладная,
    начальный долг, возврат денег от поставщика), минус уменьшает (оплата,
    возврат товара). Накладная показана в ПЕРВОНАЧАЛЬНОЙ сумме — возвраты идут
    отдельными строками, иначе их не видно.
    """
    rows = []
    for d in supplier.opening_debts.all():
        rows.append({
            "date": d.as_of, "order": 0, "id": d.id, "type": "OPENING",
            "doc": "Начальный долг", "delta": d.amount, "note": d.note,
            "who": d.created_by.username if d.created_by_id else "",
        })
    for s in supplier.supplies.all():
        if s.is_opening:
            continue
        # Накладная — в ПЕРВОНАЧАЛЬНОЙ сумме: возврат на месте её уменьшил,
        # возврат датой возврата (закрытый месяц) — нет.
        returned = sum((r.amount for r in s.returns.all() if r.in_place), ZERO)
        label = f"Накладная {_label(s)}"
        if s.is_foreign:
            label += f" ({s.total_foreign} {s.currency} по {s.rate})"
        rows.append({
            "date": s.received_on, "order": 1, "id": s.id, "type": "INVOICE", "supply": s.id,
            "doc": label, "delta": s.total_cost + returned, "note": s.note,
            "who": s.created_by.username if s.created_by_id else "",
        })
        for r in s.returns.all():
            rows.append({
                "date": r.returned_on, "order": 2, "id": r.id, "type": "RETURN", "supply": s.id,
                "doc": f"Возврат товара по накладной {_label(s)}", "delta": -r.amount,
                "note": r.note, "who": r.created_by.username if r.created_by_id else "",
            })
        if s.paid_amount:
            rows.append({
                "date": s.received_on, "order": 3, "id": s.id, "type": "LEGACY_PAYMENT",
                "supply": s.id, "doc": f"Оплата при приёмке накладной {_label(s)}",
                "delta": -s.paid_amount, "account": s.paid_account, "cash": s.paid_amount,
                "note": "", "who": "",
            })
    for p in supplier.payments.all():
        if p.kind == SupplierPayment.Kind.OFFSET:
            continue
        refund = p.kind == SupplierPayment.Kind.REFUND
        where = f" по накладной {_label(p.supply)}" if p.supply_id else " (аванс)"
        rows.append({
            "date": p.paid_on, "order": 4, "id": p.id, "type": p.kind, "payment": p.id,
            "supply": p.supply_id,
            "doc": ("Возврат денег от поставщика" if refund else "Оплата") + where,
            "delta": p.settled if refund else -p.settled,
            "account": p.account, "cash": p.amount, "fx_diff": p.fx_diff,
            "currency": p.currency, "amount_fc": p.amount_fc, "rate": p.rate,
            "note": p.note, "who": p.created_by.username if p.created_by_id else "",
        })
    rows.sort(key=lambda r: (r["date"], r["order"], r["id"]))
    running = ZERO
    opening_balance = ZERO
    out = []
    for r in rows:
        running += r["delta"]
        if d_from and r["date"] < d_from:
            opening_balance = running
            continue
        if d_to and r["date"] > d_to:
            continue
        out.append({**r, "balance": running})
    closing = running if not d_to else (out[-1]["balance"] if out else opening_balance)
    if not d_from:
        opening_balance = ZERO
    return {
        "supplier": supplier.id, "supplier_name": supplier.name,
        "from": d_from, "to": d_to,
        "opening_balance": opening_balance, "closing_balance": closing,
        "rows": out,
    }


STATEMENT_HEAD = [
    "Дата", "Тип", "Документ", "Начислено (+) / оплачено (−), сом", "Остаток долга, сом",
    "Счёт", "Деньги по кассе, сом", "Курсовая разница, сом", "Примечание", "Кто",
]
TYPE_NAMES = {
    "OPENING": "Начальный долг", "INVOICE": "Накладная", "RETURN": "Возврат товара",
    "LEGACY_PAYMENT": "Оплата при приёмке", "PAYMENT": "Оплата", "REFUND": "Возврат денег",
}


def statement_csv(data: dict) -> str:
    """CSV для русского Excel: разделитель «;», числа с запятой, BOM ставит вьюха."""
    def num(v):
        return "" if v in (None, "") else str(Decimal(v)).replace(".", ",")

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([f"Выписка по поставщику {data['supplier_name']}"])
    w.writerow(["Остаток на начало", num(data["opening_balance"])])
    w.writerow(STATEMENT_HEAD)
    for r in data["rows"]:
        w.writerow([
            r["date"].strftime("%d.%m.%Y"), TYPE_NAMES.get(r["type"], r["type"]), r["doc"],
            num(r["delta"]), num(r["balance"]), r.get("account", ""), num(r.get("cash")),
            num(r.get("fx_diff")) if r.get("fx_diff") else "", r.get("note", ""), r.get("who", ""),
        ])
    w.writerow(["Остаток на конец", num(data["closing_balance"])])
    return buf.getvalue()


def payments_csv(rows) -> str:
    def num(v):
        return "" if v in (None, "") else str(Decimal(v)).replace(".", ",")

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Дата", "Поставщик", "Накладная", "Вид", "Сумма, сом", "Курсовая разница, сом",
                "Счёт", "Валюта", "Сумма в валюте", "Курс", "Примечание", "Кто"])
    for p in rows:
        w.writerow([
            p.paid_on.strftime("%d.%m.%Y"), p.supplier.name if p.supplier_id else "",
            _label(p.supply) if p.supply_id else "аванс", p.get_kind_display(), num(p.amount),
            num(p.fx_diff) if p.fx_diff else "", p.account, p.currency if p.amount_fc is not None else "",
            num(p.amount_fc), num(p.rate), p.note, p.created_by.username if p.created_by_id else "",
        ])
    return buf.getvalue()
