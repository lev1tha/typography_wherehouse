"""ОДДС — отчёт о движении денежных средств, кассовый прямой метод.

Источник — кассовая книга (`CashEntry`): каждая запись — реальные деньги в кассе
или на счёте. Куда запись ложится, решает справочник `finance.chart`:

- три вида деятельности — операционная, инвестиционная, финансовая; их сумма и
  есть ЧИСТЫЙ ДЕНЕЖНЫЙ ПОТОК;
- «вне потока» — переводы между кассой и банком и ввод начального остатка: итог
  денег они не меняют (перевод) или это не движение вовсе (остаток), поэтому в
  поток не входят (D-5, D-6).

Тождество, которое держит отчёт — в целом и по каждому счёту:

    остаток на начало + чистый поток + вне потока = остаток на конец

Перевод вносят двумя записями (D-6). Если вторую половину забыли, сумма
переводов не ноль — отчёт показывает «не сведены: N» с предупреждением, а не
прячет разницу в потоке.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta

from django.utils import timezone

from .. import chart
from ..models import CashEntry, ExpenseKind
from ..periods import month_end
from .money import ZERO, total

ACCOUNTS = CashEntry.Account.values

# Порядок строк внутри раздела; виды расхода — между «поставщиками» и
# «процентами», по блоку и порядку вида.
LINE_ORDER = {
    "clients": 10, "unpay": 15, "refunds": 20, "suppliers": 30,
    "interest_paid": 70, "tax_paid": 75, "cash_count": 80,
    "expense_manual": 90, "salary_manual": 91, "other": 95,
    "owner_in": 10, "owner_out": 20, "loan_in": 30, "loan_out": 40,
    "transfer": 10, "opening": 20,
}
BLOCK_ORDER = {
    ExpenseKind.Block.MATERIALS: 40, ExpenseKind.Block.FIXED: 45,
    ExpenseKind.Block.VARIABLE: 50, ExpenseKind.Block.INVESTMENT: 55,
    ExpenseKind.Block.BELOW: 60,
}


def classify(entry):
    """(раздел, ключ строки, подпись, порядок) или None, если денег нет."""
    mapping = chart.for_cash_entry(entry)
    if mapping.cash_section is None:
        return None
    if entry.expense_id and mapping.cash_line is None:
        kind = entry.expense.kind
        order = BLOCK_ORDER.get(kind.block, 65) + kind.position / 1000
        return mapping.cash_section, f"kind:{kind.id}", kind.name, order
    return mapping.cash_section, mapping.cash_line, mapping.cash_label, LINE_ORDER.get(mapping.cash_line, 99)


def entries(d_from=None, d_to=None):
    qs = CashEntry.objects.select_related("expense__kind")
    if d_from:
        qs = qs.filter(happened_on__gte=d_from)
    if d_to:
        qs = qs.filter(happened_on__lte=d_to)
    return qs


def _balances(day) -> dict:
    """Остаток по счетам на конец дня (`None` — на сейчас), до начала — 0."""
    return {acc: CashEntry.balance(acc, upto=day) for acc in ACCOUNTS}


def cash_flow(d_from=None, d_to=None) -> dict:
    """ОДДС за период: разделы со строками, поток, вне потока, остатки."""
    lines = OrderedDict()
    for e in entries(d_from, d_to):
        place = classify(e)
        if place is None:
            continue
        section, key, label, order = place
        slot = lines.setdefault((section, key), {
            "section": section, "key": key, "label": label, "order": order,
            "amount": ZERO, "by_account": {acc: ZERO for acc in ACCOUNTS},
        })
        slot["amount"] += e.signed_amount
        slot["by_account"][e.account] += e.signed_amount

    sections = OrderedDict()
    for section in (*chart.FLOW_SECTIONS, chart.OUTSIDE):
        mine = sorted(
            (s for s in lines.values() if s["section"] == section),
            key=lambda s: (s["order"], s["label"]),
        )
        sections[section] = {
            "label": chart.CASH_SECTIONS[section],
            "total": total(s["amount"] for s in mine),
            "by_account": {acc: total(s["by_account"][acc] for s in mine) for acc in ACCOUNTS},
            "lines": mine,
        }

    net = total(sections[s]["total"] for s in chart.FLOW_SECTIONS)
    outside = sections[chart.OUTSIDE]["total"]
    transfers = lines.get((chart.OUTSIDE, "transfer"), {}).get("amount", ZERO)

    opening = _balances(d_from - timedelta(days=1)) if d_from else {acc: ZERO for acc in ACCOUNTS}
    closing = _balances(d_to)
    by_account = {}
    for acc in ACCOUNTS:
        flow = total(sections[s]["by_account"][acc] for s in chart.FLOW_SECTIONS)
        out = sections[chart.OUTSIDE]["by_account"][acc]
        by_account[acc] = {
            "opening": opening[acc], "flow": flow, "outside": out, "closing": closing[acc],
            "balanced": opening[acc] + flow + out == closing[acc],
        }

    clients = lines.get((chart.OPERATING, "clients"))
    return {
        "period": {"from": d_from, "to": d_to},
        "opening": total(opening.values()),
        "sections": sections,
        "net_flow": net,
        "outside": outside,
        # Сумма переводов за период: ноль — обе половины на месте.
        "unmatched_transfers": transfers,
        "closing": total(closing.values()),
        "by_account": by_account,
        "balanced": all(a["balanced"] for a in by_account.values()),
        # «Получено от клиентов» — деньги, а не заказы: оплаты минус выданная
        # сдача, по кассовой книге (откаты — своей строкой, D-18).
        "received_from_clients": {
            "total": clients["amount"] if clients else ZERO,
            "by_account": clients["by_account"] if clients else {acc: ZERO for acc in ACCOUNTS},
        },
    }


def cash_flow_year(year: int) -> dict:
    """Таблица ОДДС: строки × 12 месяцев + итог года."""
    today = timezone.localdate()
    months = [(date(year, m, 1), month_end(date(year, m, 1))) for m in range(1, 13)]
    per = [cash_flow(first, last) for first, last in months]

    # (раздел, ключ) → подпись, порядок, значения по месяцам
    table = OrderedDict()
    for i, cf in enumerate(per):
        for section, data in cf["sections"].items():
            for line in data["lines"]:
                slot = table.setdefault((section, line["key"]), {
                    "label": line["label"], "order": line["order"], "values": [ZERO] * 12,
                })
                slot["values"][i] = line["amount"]
    # Строки, которые владелец ждёт видеть всегда — даже пустыми.
    for key, label in (("clients", "Поступления от клиентов"),
                       ("suppliers", "Оплата поставщикам за материал")):
        table.setdefault((chart.OPERATING, key), {
            "label": label, "order": LINE_ORDER[key], "values": [ZERO] * 12,
        })

    rows = []

    def add(key, label, values, kind="row", level=1, total_=None, hint=None, warn=None):
        rows.append({
            "key": key, "label": label, "kind": kind, "level": level, "values": values,
            "total": total(values) if total_ is None else total_, "hint": hint, "warn": warn,
        })

    opening = [cf["opening"] for cf in per]
    add("opening", "Остаток на начало", opening, kind="balance", level=0,
        total_=opening[0], hint="cash_opening")
    for section in chart.FLOW_SECTIONS:
        mine = sorted(
            ((k, s) for (sec, k), s in table.items() if sec == section),
            key=lambda kv: (kv[1]["order"], kv[1]["label"]),
        )
        if not mine and section != chart.OPERATING:
            continue
        add(f"section:{section}", chart.CASH_SECTIONS[section],
            [cf["sections"][section]["total"] for cf in per], kind="subtotal", level=0,
            hint=f"cash_{section}")
        for key, slot in mine:
            add(f"{section}:{key}", slot["label"], slot["values"])
    add("net", "Чистый денежный поток", [cf["net_flow"] for cf in per],
        kind="total", level=0, hint="net_cash_flow")

    outside = sorted(
        ((k, s) for (sec, k), s in table.items() if sec == chart.OUTSIDE),
        key=lambda kv: kv[1]["order"],
    )
    if outside:
        add(f"section:{chart.OUTSIDE}", chart.CASH_SECTIONS[chart.OUTSIDE],
            [cf["outside"] for cf in per], kind="subtotal", level=0, hint="cash_outside")
        for key, slot in outside:
            add(f"{chart.OUTSIDE}:{key}", slot["label"], slot["values"])
        unmatched = [cf["unmatched_transfers"] for cf in per]
        if any(unmatched):
            add("unmatched_transfers", "Переводы не сведены", unmatched, level=1,
                warn="Сумма переводов за месяц не ноль: внесена только одна половина перевода "
                     "(или перевод растянулся на два месяца). Проверьте кассовую книгу.")

    closing = [cf["closing"] for cf in per]
    add("closing", "Остаток на конец", closing, kind="grand", level=0, total_=closing[-1],
        hint="cash_closing")
    labels = dict(CashEntry.Account.choices)
    for acc in ACCOUNTS:
        values = [cf["by_account"][acc]["closing"] for cf in per]
        add(f"closing:{acc}", f"в т.ч. {str(labels[acc]).lower()}", values, total_=values[-1])

    return {
        "year": year,
        "months": [
            {"month": first.month, "from": first, "to": last, "future": first > today}
            for first, last in months
        ],
        "rows": rows,
        "balanced": all(cf["balanced"] for cf in per),
    }
