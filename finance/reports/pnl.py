"""ОПиУ — отчёт о прибылях и убытках, метод начисления.

Структура (решения владельца D-1…D-32, план — docs/FINANCE_PLAN.md):

    Выручка                         (материал · резка · прочие работы)
  − Себестоимость                   (материал · расходники услуг · потери материала, D-15)
  = Валовая прибыль
  − Операционные расходы            (по «за какой месяц», D-2; по блокам и статьям)
  − Расходы, внесённые прямо в кассе (старые, до 27.09.2026, D-28)
  ± Недостача / излишек кассы       (D-5)
  = EBITDA
  − Амортизация и списание выбывшего (D-1, D-20)
  = Операционная прибыль
  − Проценты по займам              (D-4)
  − Налог с выручки                 (ставка истории × выручка, D-10, D-19)
  = Чистая прибыль

Где у чего дата:
- выручка и себестоимость — день признания выручки, возврат — день возврата
  (`sales.reporting`); потери — день записи журнала склада; касса — её день;
- то, что ложится в прибыль МЕСЯЦЕМ (операционные расходы и проценты по «за какой
  месяц», амортизация, налог), раскладывается по дням месяца (`month_alloc`):
  расход, оплаченный в своём месяце, — на день оплаты (постоянные — поровну по
  дням, как и раньше: аренду «отрабатывают» каждый день), иначе поровну;
  амортизация — поровну, списание выбывшего — последним днём; налог — по дням от
  дневной выручки. Отчёт за любой период — сумма дней, поэтому прибыль месяца,
  года и графика по дням — одно и то же число.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Min, Q
from django.utils import timezone

from sales import reporting
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog

from .. import chart
from ..models import CashEntry, ExpenseEntry, ExpenseKind, TaxRate
from ..periods import add_months, local_day, month_end, month_start
from . import depreciation
from .money import SUM, ZERO, cumulative_split, pct, split_evenly, total

# Порядок блоков операционных расходов. «Инвестиции» здесь — покупки дешевле
# порога капвложения: они расход месяца (IAS 7, п. 16; D-22).
OPEX_BLOCKS = (
    ExpenseKind.Block.MATERIALS,
    ExpenseKind.Block.FIXED,
    ExpenseKind.Block.VARIABLE,
    ExpenseKind.Block.INVESTMENT,
)
BLOCK_LABELS = {
    ExpenseKind.Block.MATERIALS: "Материалы (транспорт и прочее)",
    ExpenseKind.Block.FIXED: "Постоянные расходы",
    ExpenseKind.Block.VARIABLE: "Переменные расходы",
    ExpenseKind.Block.INVESTMENT: "Покупки дешевле порога капвложения",
}

A = CashEntry.Article


# --- Период -------------------------------------------------------------------


def earliest_day() -> date | None:
    """Самый ранний день, с которого в системе есть деньги для ОПиУ."""
    candidates = [
        Receipt.objects.filter(revenue_recognized_at__isnull=False).aggregate(
            m=Min("revenue_recognized_at"))["m"],
        ExpenseEntry.objects.aggregate(m=Min("period"))["m"],
        ExpenseEntry.objects.aggregate(m=Min("spent_at"))["m"],
        CashEntry.objects.aggregate(m=Min("happened_on"))["m"],
        InventoryLog.objects.aggregate(m=Min("happened_at"))["m"],
    ]
    days = [local_day(c) for c in candidates if c]
    return min(days) if days else None


def resolve(d_from=None, d_to=None) -> tuple[date, date]:
    """Границы периода. Нет конца — конец текущего месяца (текущий месяц в
    «весь период» входит целиком, как и в отчёт за этот месяц); нет начала —
    первый день, с которого есть данные."""
    if d_to is None:
        d_to = month_end(timezone.localdate())
    if d_from is None:
        d_from = earliest_day() or month_start(d_to)
    if d_from > d_to:
        d_from = d_to
    return d_from, d_to


def months_in(d_from, d_to) -> list[date]:
    out, m = [], month_start(d_from)
    while m <= d_to:
        out.append(m)
        m = add_months(m, 1)
    return out


def days_of(month) -> list[date]:
    first = month_start(month)
    return [first + timedelta(days=i) for i in range(month_end(first).day)]


# --- Строки, у которых своя дата ------------------------------------------------


def lines_money(d_from, d_to, **flt):
    """Выручка и себестоимость части строк чеков за период — тем же правилом,
    что выручка целиком: строки продаж периода + возвращённые позже периода −
    возвращённые в нём."""
    base = reporting.sold_lines(TransactionItem.objects.filter(is_returned=False, **flt))
    base = reporting._between(base, reporting.LINE_SOLD_ON, d_from, d_to)
    back = reporting.added_back(d_from, d_to).filter(**flt)
    out = reporting.returned_lines(d_from, d_to).filter(**flt)
    money = reporting.money(base) + reporting.money(back) - reporting.money(out)
    cost = reporting.cost(base) + reporting.cost(back) - reporting.cost(out)
    return money, cost


def losses_qs(d_from=None, d_to=None):
    """Брак и недостача: правки остатка и списания в минус."""
    qs = InventoryLog.objects.filter(
        type__in=[InventoryLog.Type.ADJUSTMENT, InventoryLog.Type.WRITE_OFF],
        quantity_changed__lt=0,
    )
    if d_from:
        qs = qs.filter(happened_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(happened_at__date__lte=d_to)
    return qs


def losses(d_from=None, d_to=None):
    """Потери по себестоимости — (сумма, записей без себестоимости).

    Записи до 04.09.2026 себестоимости не знают — их в сумме нет, отчёт
    показывает их число (аудит, Б-14)."""
    qs = losses_qs(d_from, d_to)
    return (
        qs.filter(cost__isnull=False).aggregate(v=SUM("cost"))["v"],
        qs.filter(cost__isnull=True).count(),
    )


def _cash(d_from, d_to, **flt):
    return CashEntry.objects.filter(happened_on__gte=d_from, happened_on__lte=d_to, **flt)


def _signed(qs) -> Decimal:
    v = ZERO
    for kind, amount in qs.values_list("kind", "amount"):
        v += amount if kind == CashEntry.Kind.IN else -amount
    return v


def manual_cash_expenses(d_from, d_to):
    """Старые расходы, внесённые прямо в кассу (D-28), — положительной суммой."""
    return -_signed(_cash(d_from, d_to, article__in=[A.EXPENSE, A.SALARY], expense__isnull=True))


def cash_count(d_from, d_to):
    """Пересчёт кассы со знаком: излишек — плюс, недостача — минус (D-5)."""
    return _signed(_cash(d_from, d_to, article=A.COUNT))


# --- То, что ложится в прибыль месяцем -------------------------------------------


def month_alloc(month) -> dict:
    """{ключ: {день: сумма}} месяца для строк, у которых дата — месяц.

    Ключи: ("opex", id вида), "interest", "depreciation", "disposal", "tax".
    """
    month = month_start(month)
    days = days_of(month)
    alloc = defaultdict(lambda: defaultdict(lambda: ZERO))

    def spread(key, amount):
        for day, part in zip(days, split_evenly(amount, len(days))):
            alloc[key][day] += part

    entries = ExpenseEntry.objects.filter(
        Q(period=month) | Q(period__isnull=True, spent_at__gte=month, spent_at__lte=days[-1])
    ).select_related("kind")
    for entry in entries:
        mapping = chart.for_expense(entry)
        if mapping.pnl == chart.OPEX:
            key = ("opex", entry.kind_id)
        elif mapping.pnl == chart.INTEREST:
            key = "interest"
        else:
            continue    # актив (амортизация ниже), уплата налога, закуп, справочные
        paid_in_month = month_start(entry.spent_at) == month
        if paid_in_month and entry.kind.block != ExpenseKind.Block.FIXED:
            alloc[key][entry.spent_at] += entry.amount
        else:
            spread(key, entry.amount)

    dep = depreciation.by_month(month, month).get(month)
    if dep:
        spread("depreciation", dep["depreciation"])
        if dep["disposal"]:
            alloc["disposal"][days[-1]] += dep["disposal"]

    rate = TaxRate.rate_for(month)
    if rate:
        revenue_by_day, _ = reporting.by_day(month, days[-1])
        exact = [rate * revenue_by_day.get(day, ZERO) / 100 for day in days]
        for day, part in zip(days, cumulative_split(exact)):
            if part:
                alloc["tax"][day] += part
    return alloc


def _alloc_in(d_from, d_to):
    """Сумма раскладок месяцев по дням периода: {ключ: сумма} и ставки месяцев."""
    sums = defaultdict(lambda: ZERO)
    rates = set()
    for month in months_in(d_from, d_to):
        rates.add(TaxRate.rate_for(month))
        for key, by_day in month_alloc(month).items():
            for day, value in by_day.items():
                if d_from <= day <= d_to:
                    sums[key] += value
    return sums, rates


# --- ОПиУ за период --------------------------------------------------------------


def _opex_blocks(sums):
    """Строки операционных расходов по блокам и видам (только ненулевые)."""
    kind_ids = [key[1] for key in sums if isinstance(key, tuple) and key[0] == "opex"]
    kinds = {k.id: k for k in ExpenseKind.objects.filter(id__in=kind_ids)}
    blocks = []
    for block in OPEX_BLOCKS:
        rows = [
            {"kind_id": kid, "name": kinds[kid].name, "position": kinds[kid].position,
             "amount": sums[("opex", kid)]}
            for kid in kinds if kinds[kid].block == block and sums[("opex", kid)]
        ]
        rows.sort(key=lambda r: (r["position"], r["kind_id"]))
        if rows:
            blocks.append({
                "block": block, "label": BLOCK_LABELS[block],
                "total": total(r["amount"] for r in rows), "rows": rows,
            })
    return blocks


def tax_label(rates) -> str:
    rates = {r for r in rates if r}
    if len(rates) == 1:
        rate = rates.pop()
        return chart.PNL_LINES[chart.TAX].format(rate=format(rate.normalize(), "f"))
    return "Налог с выручки"


def pnl(d_from=None, d_to=None) -> dict:
    """ОПиУ за период. Расходные строки — положительными суммами."""
    d_from, d_to = resolve(d_from, d_to)

    revenue = reporting.revenue(d_from, d_to)
    revenue_material, cogs_material = lines_money(d_from, d_to, type=TransactionItem.Type.MATERIAL)
    revenue_cutting, _ = lines_money(
        d_from, d_to, type=TransactionItem.Type.SERVICE, service__kind="CUTTING"
    )
    cogs = reporting.cogs(d_from, d_to)
    loss, loss_unknown = losses(d_from, d_to)
    cogs_total = cogs + loss
    gross = revenue - cogs_total

    sums, rates = _alloc_in(d_from, d_to)
    blocks = _opex_blocks(sums)
    opex = total(b["total"] for b in blocks)
    manual = manual_cash_expenses(d_from, d_to)
    count = cash_count(d_from, d_to)
    ebitda = gross - opex - manual + count

    dep = sums["depreciation"]
    disposal = sums["disposal"]
    operating = ebitda - dep - disposal
    interest = sums["interest"]
    tax = sums["tax"]
    net = operating - interest - tax

    capex = ExpenseEntry.objects.filter(
        kind__role=ExpenseKind.Role.CAPEX, useful_life_months__isnull=False,
        spent_at__gte=d_from, spent_at__lte=d_to,
    ).aggregate(v=SUM("amount"))["v"]

    return {
        "period": {"from": d_from, "to": d_to},
        "revenue": revenue,
        "revenue_material": revenue_material,
        "revenue_cutting": revenue_cutting,
        "revenue_other": revenue - revenue_material - revenue_cutting,
        "cogs_material": cogs_material,
        "cogs_services": cogs - cogs_material,
        "losses": loss,
        "losses_unknown": loss_unknown,
        "cogs_total": cogs_total,
        "gross_profit": gross,
        "opex": {"total": opex, "blocks": blocks},
        "opex_cash_manual": manual,
        "cash_count": count,
        "ebitda": ebitda,
        "depreciation": dep,
        "disposal": disposal,
        "operating_profit": operating,
        "interest": interest,
        "tax": tax,
        "tax_label": tax_label(rates),
        "net_profit": net,
        "margins": {
            "gross": pct(gross, revenue),
            "ebitda": pct(ebitda, revenue),
            "operating": pct(operating, revenue),
            "net": pct(net, revenue),
        },
        # Справочно: сколько вложено в активы (в прибыль — амортизацией).
        "capex_purchases": capex,
    }


# --- ОПиУ по дням (график) ---------------------------------------------------------


def pnl_by_day(d_from, d_to) -> list[dict]:
    """Строки ОПиУ по дням периода: та же арифметика, что `pnl`, день за днём."""
    revenue, cogs = reporting.by_day(d_from, d_to)
    loss = defaultdict(lambda: ZERO)
    for log in losses_qs(d_from, d_to).filter(cost__isnull=False).only("happened_at", "cost"):
        loss[local_day(log.happened_at)] += log.cost
    manual = defaultdict(lambda: ZERO)
    count = defaultdict(lambda: ZERO)
    for e in _cash(d_from, d_to, article__in=[A.EXPENSE, A.SALARY, A.COUNT]).only(
        "article", "kind", "amount", "happened_on", "expense_id"
    ):
        if e.article == A.COUNT:
            count[e.happened_on] += e.signed_amount
        elif not e.expense_id:
            manual[e.happened_on] -= e.signed_amount
    alloc = defaultdict(lambda: defaultdict(lambda: ZERO))
    for month in months_in(d_from, d_to):
        for key, by_day in month_alloc(month).items():
            name = "opex" if isinstance(key, tuple) else key
            for day, value in by_day.items():
                alloc[name][day] += value

    rows = []
    day = d_from
    while day <= d_to:
        row = {
            "date": day,
            "revenue": revenue.get(day, ZERO),
            "cogs": cogs.get(day, ZERO),
            "losses": loss.get(day, ZERO),
            "opex": alloc["opex"].get(day, ZERO),
            "opex_cash_manual": manual.get(day, ZERO),
            "cash_count": count.get(day, ZERO),
            "depreciation": alloc["depreciation"].get(day, ZERO) + alloc["disposal"].get(day, ZERO),
            "interest": alloc["interest"].get(day, ZERO),
            "tax": alloc["tax"].get(day, ZERO),
        }
        row["net_profit"] = (
            row["revenue"] - row["cogs"] - row["losses"] - row["opex"] - row["opex_cash_manual"]
            + row["cash_count"] - row["depreciation"] - row["interest"] - row["tax"]
        )
        rows.append(row)
        day += timedelta(days=1)
    return rows


# --- ОПиУ по месяцам года (таблица) -------------------------------------------------


def pnl_year(year: int) -> dict:
    """Таблица ОПиУ: строки × 12 месяцев + итог года. Расходы — со знаком минус."""
    from datetime import date as _date

    today = timezone.localdate()
    months = [(_date(year, m, 1), month_end(_date(year, m, 1))) for m in range(1, 13)]
    per = [pnl(first, last) for first, last in months]

    rows = []

    def add(key, label, values, kind="row", level=1, hint=None):
        rows.append({
            "key": key, "label": label, "kind": kind, "level": level, "hint": hint,
            "values": values,
            "total": total(values) if kind != "percent" else None,
        })

    def col(fn):
        return [fn(p) for p in per]

    def neg(fn):
        return [-fn(p) for p in per]

    revenue = col(lambda p: p["revenue"])
    add("revenue", "Выручка", revenue, kind="total", level=0, hint="revenue")
    add("revenue_material", "Материал", col(lambda p: p["revenue_material"]))
    add("revenue_cutting", "Резка (ЧПУ и лазер)", col(lambda p: p["revenue_cutting"]))
    add("revenue_other", "Прочие работы и услуги", col(lambda p: p["revenue_other"]))

    add("cogs", "Себестоимость", neg(lambda p: p["cogs_total"]), kind="subtotal", level=0, hint="cogs")
    add("cogs_material", chart.PNL_LINES[chart.COGS_MATERIAL], neg(lambda p: p["cogs_material"]))
    add("cogs_services", chart.PNL_LINES[chart.COGS_SERVICES], neg(lambda p: p["cogs_services"]))
    add("losses", chart.PNL_LINES[chart.LOSSES], neg(lambda p: p["losses"]), hint="losses")

    gross = col(lambda p: p["gross_profit"])
    add("gross", "Валовая прибыль", gross, kind="total", level=0, hint="gross")
    add("gross_pct", "Валовая маржа, %", [pct(g, r) for g, r in zip(gross, revenue)], kind="percent")

    # Операционные расходы: все действующие статьи блоков «Материалы»,
    # «Постоянные», «Переменные» — даже пустые, как строки в Excel владельца
    # (так было и до 2026-10-07), — плюс скрытые и «Инвестиции дешевле порога»,
    # если по ним в году что-то было.
    kind_values = defaultdict(lambda: [ZERO] * 12)
    kind_meta = {}
    for k in ExpenseKind.objects.filter(
        role=ExpenseKind.Role.OPEX, is_archived=False,
        block__in=[ExpenseKind.Block.MATERIALS, ExpenseKind.Block.FIXED, ExpenseKind.Block.VARIABLE],
    ):
        kind_meta[k.id] = (k.block, k.name, k.position)
    for i, p in enumerate(per):
        for block in p["opex"]["blocks"]:
            for r in block["rows"]:
                kind_values[r["kind_id"]][i] = -r["amount"]
                kind_meta[r["kind_id"]] = (block["block"], r["name"], r["position"])
    add("opex", "Операционные расходы", neg(lambda p: p["opex"]["total"]),
        kind="subtotal", level=0, hint="opex")
    for block in OPEX_BLOCKS:
        mine = sorted(
            (kid for kid, meta in kind_meta.items() if meta[0] == block),
            key=lambda kid: (kind_meta[kid][2], kid),
        )
        if not mine:
            continue
        block_total = [total(col_) for col_ in zip(*(kind_values[k] for k in mine))]
        add(f"block:{block}", BLOCK_LABELS[block], block_total, kind="group", level=1)
        for kid in mine:
            add(f"kind:{kid}", kind_meta[kid][1], kind_values[kid], level=2)
    manual = neg(lambda p: p["opex_cash_manual"])
    if any(manual):
        add("opex_cash_manual", chart.PNL_LINES[chart.OPEX_CASH_MANUAL], manual, level=0)
    count = col(lambda p: p["cash_count"])
    if any(count):
        add("cash_count", chart.PNL_LINES[chart.CASH_COUNT], count, level=0, hint="cash_count")

    ebitda = col(lambda p: p["ebitda"])
    add("ebitda", "EBITDA (прибыль до амортизации, процентов и налога)", ebitda,
        kind="total", level=0, hint="ebitda")
    add("ebitda_pct", "Маржа EBITDA, %", [pct(e, r) for e, r in zip(ebitda, revenue)], kind="percent")

    add("depreciation_total", "Амортизация", neg(lambda p: p["depreciation"] + p["disposal"]),
        kind="subtotal", level=0, hint="depreciation")
    add("depreciation", "Амортизация оборудования и цеха", neg(lambda p: p["depreciation"]))
    disposal = neg(lambda p: p["disposal"])
    if any(disposal):
        add("disposal", chart.PNL_LINES[chart.DISPOSAL], disposal, hint="disposal")

    operating = col(lambda p: p["operating_profit"])
    add("operating", "Операционная прибыль", operating, kind="total", level=0, hint="operating")
    add("operating_pct", "Операционная маржа, %",
        [pct(o, r) for o, r in zip(operating, revenue)], kind="percent")

    add("interest", chart.PNL_LINES[chart.INTEREST], neg(lambda p: p["interest"]),
        level=0, hint="interest")
    year_rates = {TaxRate.rate_for(first) for first, _ in months}
    add("tax", tax_label(year_rates), neg(lambda p: p["tax"]), level=0, hint="tax")

    net = col(lambda p: p["net_profit"])
    add("net", "Чистая прибыль", net, kind="grand", level=0, hint="net")
    add("net_pct", "Чистая маржа, %", [pct(n, r) for n, r in zip(net, revenue)], kind="percent")

    add("capex", "Справочно: вложено в оборудование и цех (в прибыль — амортизацией)",
        col(lambda p: p["capex_purchases"]), kind="note", level=0, hint="capex")

    # Проценты итога — от итогов года, а не суммой месяцев.
    totals = {r["key"]: r["total"] for r in rows if r["total"] is not None}
    for key, base in (("gross_pct", "gross"), ("ebitda_pct", "ebitda"),
                      ("operating_pct", "operating"), ("net_pct", "net")):
        next(r for r in rows if r["key"] == key)["total"] = pct(totals[base], totals["revenue"])

    return {
        "year": year,
        "months": [
            {"month": first.month, "from": first, "to": last, "future": first > today}
            for first, last in months
        ],
        "rows": rows,
        "losses_unknown": sum(p["losses_unknown"] for p in per),
    }
