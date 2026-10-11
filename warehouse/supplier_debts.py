"""Долг поставщикам по одиночным приходам — одним расчётом для карточки и сверки.

Решение владельца 11.10 (D-195): ПРИХОД БЕЗ УКАЗАННОЙ ОПЛАТЫ — ЭТО ДОЛГ.

Сверка «прибыль → деньги» («Обзор») всегда считала так: закуп за период минус
оплаты поставщикам из кассы. Карточка «Долг поставщикам» видела только
накладные и партии с отметкой «в долг» (`Roll.supplier_debt`), а приход, у
которого способ оплаты не назвали (все приходы до 27.09, одиночная кнопка без
выбора), считала оплаченным. Одна и та же строка «Долг поставщикам» на демо
давала +2 306 877 в сверке и 22 560 в карточке.

Теперь у одиночной партии (не из накладной) долг считается одинаково для всех:

    долг партии = сумма закупки − заплачено по ней через кассу

У партии «в долг» это ровно `supplier_debt`, у оплаченной при приёмке — ноль, а
у старой партии без оплаты и без отметки — вся её сумма. Данные не
мигрируются: отметки в базе остаются как были, долг выводится расчётом.
Оплата такой партии — тем же путём, что партии «в долг» (`pay_lot_supplier`).

Что ещё сверка считает долгом и оплатой, а карточка раньше не видела:

* ПРИХОД БЕЗ ПАРТИИ — запись журнала «Поступление» с ценой, но без партии и
  накладной (быстрый приход штучного до 27.08, крепёж демо-данных). Закуп
  есть, документа для оплаты нет — строка карточки без кнопки «Оплатить».
* ОПЛАТА БЕЗ ДОКУМЕНТА — трата вида «Закуп в склад» («Закуп материала») и
  старые ручные записи кассы «Оплата поставщику». Сначала гасят приходы без
  партии (старые вперёд), остаток — деньги у поставщиков (рядом с авансами).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from .models import InventoryLog, Roll, SupplierPayment

ZERO = Decimal("0")
CENT = Decimal("0.01")


def standalone_lots():
    """Одиночные партии: приняты кнопкой «Поступление», не накладной. Их долг
    живёт в самой партии; долг партии из накладной — в накладной."""
    return Roll.objects.filter(supply_line__isnull=True)


def lot_cash_paid(roll_ids=None) -> dict:
    """{id партии: заплачено поставщику по кассе, нетто} — только партии, у
    которых есть хоть одна запись «Оплата поставщику» (даже если в сумме 0)."""
    from finance.models import CashEntry

    qs = CashEntry.objects.filter(article=CashEntry.Article.SUPPLY, roll__isnull=False)
    if roll_ids is not None:
        qs = qs.filter(roll_id__in=list(roll_ids))
    out: dict = defaultdict(lambda: ZERO)
    for roll_id, kind, amount in qs.values_list("roll_id", "kind", "amount"):
        out[roll_id] += amount if kind == CashEntry.Kind.OUT else -amount
    return dict(out)


def is_standalone(roll: Roll) -> bool:
    return not Roll.objects.filter(pk=roll.pk, supply_line__isnull=False).exists()


def lot_debt(roll: Roll) -> Decimal:
    """Долг поставщику за одиночную партию: сумма закупки − заплачено по кассе.
    Минус — переплата (поставщику заплатили больше исправленной суммы). У партии
    из накладной — ноль: её долг в накладной."""
    if not is_standalone(roll):
        return ZERO
    return roll.purchase_cost - lot_cash_paid([roll.pk]).get(roll.pk, ZERO)


def lot_debts(rolls=None) -> dict:
    """{id партии: (долг, без отметки?)} у одиночных партий.

    «Без отметки» — старый приход: ни оплаты по кассе, ни отметки «в долг».
    Его карточка показывает с пометкой, а команда `lots_without_payment`
    перечисляет владельцу."""
    rolls = list(rolls if rolls is not None else standalone_lots())
    paid = lot_cash_paid([r.pk for r in rolls])
    return {
        r.pk: (r.purchase_cost - paid.get(r.pk, ZERO), r.supplier_debt == 0 and r.pk not in paid)
        for r in rolls
    }


# --- Запись прихода одиночной партии ---------------------------------------------


def lot_purchase_logs() -> dict:
    """{id записи «Поступление»: (id партии, сумма закупки партии)} у одиночных
    партий.

    Закуп одиночного прихода считается по журналу склада, а там только цена
    кв.м до тыйына: 12 000 за 14,884 кв.м давали 12 000,08, 20 174 за 229 250
    штук — 20 632,50. Карточка же должна поставщику ровно сумму партии. Чтобы
    сверка и карточка сходились до тыйына, закуп такой записи — сумма её
    партии.

    Запись знает свою партию с 10.10 (`InventoryLog.roll`). Старые — без
    ссылки: их находим, как «Исправить приход» (`supply_log_for_roll`), — тот
    же материал и площадь, при нескольких кандидатах ближайшая дата. Каждая
    запись и каждая партия — не больше одного раза; не нашлась — закуп
    остаётся «площадь × цена», как раньше.
    """
    lots = {
        row["id"]: row for row in standalone_lots().values(
            "id", "material_id", "initial_area", "received_at", "purchase_cost",
        )
    }
    logs = list(
        InventoryLog.objects.filter(
            type=InventoryLog.Type.SUPPLY, supply__isnull=True, quantity_changed__gt=0,
        ).order_by("id").values("id", "material_id", "quantity_changed", "happened_at", "roll_id")
    )
    out = {}
    taken = set()
    free = defaultdict(list)
    for log in logs:
        rid = log["roll_id"]
        if rid is None:
            free[(log["material_id"], log["quantity_changed"])].append(log)
        elif rid in lots and rid not in taken:
            out[log["id"]] = (rid, lots[rid]["purchase_cost"])
            taken.add(rid)
    waiting = sorted(
        (lot for rid, lot in lots.items() if rid not in taken),
        key=lambda lot: (lot["received_at"], lot["id"]),
    )
    for lot in waiting:
        candidates = free.get((lot["material_id"], lot["initial_area"]))
        if not candidates:
            continue
        best = min(candidates, key=lambda c: (
            abs((c["happened_at"] - lot["received_at"]).total_seconds()), c["id"],
        ))
        candidates.remove(best)
        out[best["id"]] = (lot["id"], lot["purchase_cost"])
    return out


def loose_purchases(mapped=None) -> list:
    """Приходы без партии: записи «Поступление» с ценой, без накладной и не
    принадлежащие одиночной партии. [{"id", "day", "material", "quantity",
    "unit", "price", "amount"}] по дате."""
    mapped = lot_purchase_logs() if mapped is None else mapped
    rows = []
    for log in InventoryLog.objects.filter(
        type=InventoryLog.Type.SUPPLY, supply__isnull=True, quantity_changed__gt=0,
        actual_price__isnull=False,
    ).select_related("material", "created_by").order_by("happened_at", "id"):
        if log.id in mapped:
            continue
        rows.append({
            "id": log.id, "day": timezone.localtime(log.happened_at).date(),
            "material": log.material.name, "quantity": log.quantity_changed,
            "unit": log.material.get_unit_display(), "price": log.actual_price,
            "amount": (log.quantity_changed * log.actual_price).quantize(CENT),
            "created_by": log.created_by.username if log.created_by_id else "",
            "reason": log.reason or "",
        })
    return rows


# --- Оплата без документа -------------------------------------------------------------


def unlinked_cash_entries():
    """Записи кассы, которыми сверка гасит долг поставщикам, а документа у них
    нет: «Оплата поставщику» без партии, накладной и строки платежа (старые
    ручные записи; встречные пары отменённых платежей дают в сумме ноль) и
    траты вида «Закуп в склад»."""
    from finance.models import CashEntry, ExpenseKind

    linked = set(
        SupplierPayment.objects.filter(cash_entry__isnull=False).values_list("cash_entry_id", flat=True)
    )
    qs = CashEntry.objects.filter(
        Q(article=CashEntry.Article.SUPPLY, roll__isnull=True, supply__isnull=True, expense__isnull=True)
        | Q(expense__kind__role=ExpenseKind.Role.INVENTORY)
    )
    return [e for e in qs.order_by("happened_on", "id") if e.pk not in linked]


def unlinked_paid() -> Decimal:
    """Сколько заплачено поставщикам без документа, нетто (плюс — ушло)."""
    from finance.models import CashEntry

    return sum(
        (e.amount if e.kind == CashEntry.Kind.OUT else -e.amount for e in unlinked_cash_entries()), ZERO,
    )


def undocumented() -> tuple[list, Decimal]:
    """Приходы без партии, погашенные оплатами без документа (старые вперёд), и
    остаток этих оплат: (строки с полями `paid` и `debt`, остаток). Остаток
    плюс — деньги у поставщиков, минус — нам вернули больше, чем ушло."""
    rows = loose_purchases()
    left = unlinked_paid()
    for row in rows:
        take = min(row["amount"], left) if left > 0 else ZERO
        row["paid"] = take
        row["debt"] = row["amount"] - take
        left -= take
    return rows, left
