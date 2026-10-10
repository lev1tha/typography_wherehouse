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

from django.db.models import Min
from django.utils import timezone

from sales import reporting
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog

from .. import chart
from ..models import CashEntry, ExpenseEntry, ExpenseKind, TaxRate
from ..periods import add_months, local_day, month_end, month_start
from . import depreciation
from .money import SUM, ZERO, cumulative_split, pct, split_evenly, total
from .quarters import add_quarters, quarter_meta
from .scope import once, report_scope

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
    (m_base, c_base), (m_back, c_back), (m_out, c_out) = (
        _money_and_cost(part) for part in (base, back, out)
    )
    return m_base + m_back - m_out, c_base + c_back - c_out


def _money_and_cost(lines):
    """Стоимость и себестоимость строк за ОДИН проход по ним. Те же значения,
    что `reporting.money` и `reporting.cost`, но те читали строки дважды
    (стоимость — в Python, себестоимость — суммой в базе)."""
    money = cost = ZERO
    for it in lines.only("quantity", "price_per_item", "cost_total"):
        money += it.sold_total
        cost += it.cost_total or ZERO
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


def surplus_qs(d_from=None, d_to=None):
    """Излишки инвентаризации и промера со стоимостью (F5/PNL-04, волна 2):
    правка остатка в плюс, у которой записана цена. Старые излишки цены не
    знают (`cost` пуст) — их здесь нет, прошлые месяцы не меняются."""
    qs = InventoryLog.objects.filter(
        type=InventoryLog.Type.ADJUSTMENT, quantity_changed__gt=0, cost__isnull=False,
    )
    if d_from:
        qs = qs.filter(happened_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(happened_at__date__lte=d_to)
    return qs


def losses(d_from=None, d_to=None):
    """Потери по себестоимости НЕТТО — (сумма, записей без себестоимости).

    Недостача и брак минус излишки периода (по дате операции): «насчитал 8 из
    10, нашёл ещё 2» не оставляет убытка (F5/PNL-04). Излишков больше, чем
    потерь, — строка уходит в минус: это доход от найденного.

    Записи до 04.09.2026 себестоимости не знают — их в сумме нет, отчёт
    показывает их число (аудит, Б-14)."""
    qs = losses_qs(d_from, d_to)
    lost = qs.filter(cost__isnull=False).aggregate(v=SUM("cost"))["v"]
    found = surplus_qs(d_from, d_to).aggregate(v=SUM("cost"))["v"]
    return (
        lost - found,
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


def _tax_rows():
    return once("tax_rates", lambda: list(
        TaxRate.objects.order_by("valid_from").values_list("valid_from", "rate", "basis")
    ))


def tax_rate_for(month) -> Decimal:
    """Ставка налога месяца, % (нет записи — 0). Тот же результат, что у
    `TaxRate.rate_for`, но таблица ставок читается один раз на отчёт: годовая
    таблица спрашивала её по три раза на каждый из 12 месяцев."""
    first = month_start(month)
    rate = ZERO
    for valid_from, value, _basis in _tax_rows():
        if valid_from <= first:
            rate = value
    return rate


def tax_basis_for(month) -> str:
    """Основа налога месяца (PNL-14): «по начислению» (D-10, по умолчанию) или
    «по кассе». Берётся из той же записи истории, что и ставка."""
    first = month_start(month)
    basis = TaxRate.Basis.ACCRUAL
    for valid_from, _value, value_basis in _tax_rows():
        if valid_from <= first:
            basis = value_basis
    return basis


def taxed_on_accrual(recognized_at, paid_on) -> bool:
    """Выручка чека уже обложена «по начислению» (RF-N6, D-163): признана в
    месяце РАНЬШЕ месяца денег, и в том месяце налог шёл от выручки со ставкой
    больше нуля. Тогда оплата этого чека в месяце «по кассе» — не новая база,
    а погашение уже обложенного долга."""
    if recognized_at is None:
        return False
    month = month_start(local_day(recognized_at))
    if month >= month_start(paid_on):
        return False
    return bool(tax_rate_for(month)) and tax_basis_for(month) == TaxRate.Basis.ACCRUAL


def _cash_base_entries(d_from, d_to):
    """Записи кассы базы «по кассе»: (запись, уже обложена по начислению?).

    Не облагаются второй раз приход, сдача и откат оплаты по чеку, выручка
    которого уже обложена «по начислению» (`taxed_on_accrual`). Возврат клиенту
    остаётся в базе: он поправляет уже обложенную выручку, как и раньше."""
    qs = _cash(d_from, d_to, article__in=[A.SALE, A.CHANGE, A.REFUND, A.UNPAY]).select_related(
        "receipt"
    ).only(
        "kind", "amount", "happened_on", "article",
        "receipt__revenue_recognized_at", "receipt__status",
    )
    for e in qs:
        receipt = e.receipt
        already = bool(
            receipt is not None and e.article != A.REFUND
            and receipt.status != Receipt.Status.CANCELLED
            and taxed_on_accrual(receipt.revenue_recognized_at, e.happened_on)
        )
        yield e, already


def cash_received_by_day(d_from, d_to) -> dict:
    """{день: деньги от клиентов нетто} по кассовой книге: оплаты минус сдача,
    возвраты и откаты — база налога «по кассе». Оплаты долгов, выручка
    которых уже обложена «по начислению», не входят (D-163)."""
    out = defaultdict(lambda: ZERO)
    for e, already in _cash_base_entries(d_from, d_to):
        if not already:
            out[e.happened_on] += e.signed_amount
    return out


@report_scope
def cash_already_taxed(d_from, d_to) -> Decimal:
    """Сколько денег периода (нетто) НЕ вошло в базу «по кассе», потому что их
    выручка уже обложена «по начислению» — только в месяцах «по кассе» со
    ставкой. Для выгрузки бухгалтеру: «смешанная основа» видна числом."""
    total = ZERO
    for month in months_in(d_from, d_to):
        if not tax_rate_for(month) or tax_basis_for(month) != TaxRate.Basis.CASH:
            continue
        first, last = max(month, d_from), min(month_end(month), d_to)
        for e, already in _cash_base_entries(first, last):
            if already:
                total += e.signed_amount
    return total


def _load_expenses():
    return list(ExpenseEntry.objects.select_related("kind"))


def _expenses_of(month, last_day):
    """Траты, относящиеся к месяцу в ОПиУ: «за какой месяц» (у старых без него —
    месяц оплаты). Таблица трат читается один раз на отчёт."""
    rows = once("expense_entries", _load_expenses)
    return [
        e for e in rows
        if e.period == month or (e.period is None and month <= e.spent_at <= last_day)
    ]


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

    for entry in _expenses_of(month, days[-1]):
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

    rate = tax_rate_for(month)
    if rate:
        if tax_basis_for(month) == TaxRate.Basis.CASH:
            revenue_by_day = cash_received_by_day(month, days[-1])
        else:
            revenue_by_day, _ = reporting.by_day(month, days[-1])
        exact = [rate * revenue_by_day.get(day, ZERO) / 100 for day in days]
        for day, part in zip(days, cumulative_split(exact)):
            if part:
                alloc["tax"][day] += part
    return alloc


def _alloc_in(d_from, d_to):
    """Сумма раскладок месяцев по дням периода: {ключ: сумма}, ставки и основы
    налога месяцев."""
    sums = defaultdict(lambda: ZERO)
    bases = set()
    rates = set()
    for month in months_in(d_from, d_to):
        rates.add(tax_rate_for(month))
        if tax_rate_for(month):
            bases.add(tax_basis_for(month))
        for key, by_day in month_alloc(month).items():
            for day, value in by_day.items():
                if d_from <= day <= d_to:
                    sums[key] += value
    return sums, rates, bases


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


def tax_label(rates, bases=None) -> str:
    rates = {r for r in rates if r}
    cash = bool(bases) and set(bases) == {TaxRate.Basis.CASH}
    if len(rates) == 1:
        rate = format(rates.pop().normalize(), "f")
        line = chart.PNL_LINES[chart.TAX_CASH if cash else chart.TAX]
        return line.format(rate=rate)
    return "Налог с полученных денег" if cash else "Налог с выручки"


@report_scope
def pnl(d_from=None, d_to=None) -> dict:
    """ОПиУ за период. Расходные строки — положительными суммами."""
    d_from, d_to = resolve(d_from, d_to)

    revenue = reporting.revenue(d_from, d_to)
    revenue_material, cogs_material = lines_money(d_from, d_to, type=TransactionItem.Type.MATERIAL)
    revenue_cutting, _ = lines_money(
        d_from, d_to, type=TransactionItem.Type.SERVICE, service__kind="CUTTING"
    )
    cogs = reporting.cogs(d_from, d_to)
    # Гарантийные переделки (волна 2, D-88): их себестоимость — своей строкой,
    # а не внутри материала и расходников. Сумма себестоимости та же: строки
    # только переложены, двойного счёта нет.
    _, cogs_warranty = lines_money(d_from, d_to, receipt__is_warranty=True)
    _, cogs_warranty_material = lines_money(
        d_from, d_to, type=TransactionItem.Type.MATERIAL, receipt__is_warranty=True
    )
    loss, loss_unknown = losses(d_from, d_to)
    cogs_total = cogs + loss
    gross = revenue - cogs_total

    sums, rates, bases = _alloc_in(d_from, d_to)
    blocks = _opex_blocks(sums)
    opex = total(b["total"] for b in blocks)
    # Часть операционных расходов, у которой нет денег (списанные безнадёжные
    # долги): сверке она нужна отдельной строкой, а не «ждущей оплаты».
    noncash_ids = once("noncash_kind_ids", lambda: list(
        ExpenseKind.objects.filter(code__in=ExpenseKind.NO_CASH_CODES).values_list("id", flat=True)
    ))
    opex_noncash = total(sums[("opex", kid)] for kid in noncash_ids)
    manual = manual_cash_expenses(d_from, d_to)
    count = cash_count(d_from, d_to)
    ebitda = gross - opex - manual + count

    dep = sums["depreciation"]
    disposal = sums["disposal"]
    operating = ebitda - dep - disposal
    interest = sums["interest"]
    tax = sums["tax"]
    net = operating - interest - tax

    capex = sum(
        (e.amount for e in once("expense_entries", _load_expenses)
         if e.kind.role == ExpenseKind.Role.CAPEX and e.useful_life_months is not None
         and d_from <= e.spent_at <= d_to),
        ZERO,
    )

    return {
        "period": {"from": d_from, "to": d_to},
        "revenue": revenue,
        "revenue_material": revenue_material,
        "revenue_cutting": revenue_cutting,
        "revenue_other": revenue - revenue_material - revenue_cutting,
        "cogs_material": cogs_material - cogs_warranty_material,
        "cogs_services": cogs - cogs_material - (cogs_warranty - cogs_warranty_material),
        "cogs_warranty": cogs_warranty,
        "losses": loss,
        "losses_unknown": loss_unknown,
        "cogs_total": cogs_total,
        "gross_profit": gross,
        "opex": {"total": opex, "blocks": blocks},
        "opex_noncash": opex_noncash,
        "opex_cash_manual": manual,
        "cash_count": count,
        "ebitda": ebitda,
        "depreciation": dep,
        "disposal": disposal,
        "operating_profit": operating,
        "interest": interest,
        "tax": tax,
        "tax_label": tax_label(rates, bases),
        "tax_basis": bases.pop() if len(bases) == 1 else ("MIXED" if bases else TaxRate.Basis.ACCRUAL),
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


@report_scope
def pnl_by_day(d_from, d_to) -> list[dict]:
    """Строки ОПиУ по дням периода: та же арифметика, что `pnl`, день за днём."""
    revenue, cogs = reporting.by_day(d_from, d_to)
    loss = defaultdict(lambda: ZERO)
    for log in losses_qs(d_from, d_to).filter(cost__isnull=False).only("happened_at", "cost"):
        loss[local_day(log.happened_at)] += log.cost
    for log in surplus_qs(d_from, d_to).only("happened_at", "cost"):
        loss[local_day(log.happened_at)] -= log.cost
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


@report_scope
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
    warranty = neg(lambda p: p["cogs_warranty"])
    if any(warranty):
        add("cogs_warranty", chart.PNL_LINES[chart.COGS_WARRANTY], warranty, hint="cogs_warranty")
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
    year_rates = {tax_rate_for(first) for first, _ in months}
    year_bases = {tax_basis_for(first) for first, _ in months if tax_rate_for(first)}
    add("tax", tax_label(year_rates, year_bases), neg(lambda p: p["tax"]), level=0, hint="tax")

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

    month_info = [
        {"month": first.month, "from": first, "to": last, "future": first > today}
        for first, last in months
    ]
    add_quarters(rows, percent={
        "gross_pct": ("gross", "revenue"), "ebitda_pct": ("ebitda", "revenue"),
        "operating_pct": ("operating", "revenue"), "net_pct": ("net", "revenue"),
    })
    return {
        "year": year,
        "months": month_info,
        "quarters": quarter_meta(month_info),
        "rows": rows,
        "losses_unknown": sum(p["losses_unknown"] for p in per),
    }
