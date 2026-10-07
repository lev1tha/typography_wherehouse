"""ОПиУ и ОДДС — два отчёта владельца по месяцам года.

ОПиУ (отчёт о прибылях и убытках) — метод начисления: выручка по заказам
(возврат — днём возврата, `sales.reporting`), себестоимость проданного, траты
«Финансов» по статьям, списание материала. Каждый месяц ОПиУ даёт РОВНО ту же
прибыль, что отчёт «Финансов» за этот месяц: одни и те же формулы, и на это
есть тест. Разница только в виде — строки статей, колонки месяцев, как в Excel.

ОДДС (отчёт о движении денежных средств) — прямой метод, по кассовой книге:
сколько денег реально пришло и ушло, по трём видам деятельности. Остаток на
начало + поток месяца = остаток на конец, в том числе по счетам.

Почему прибыль и деньги расходятся — видно, если положить отчёты рядом: заказ
в долг — выручка без денег, закуп — деньги без расхода (он станет
себестоимостью, когда материал продадут), станок — деньги без расхода
(инвестиции).
"""
from __future__ import annotations

import calendar
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from sales import reporting
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog

from .models import CashEntry, ExpenseEntry, ExpenseKind

ZERO = Decimal("0")

# Порядок блоков расходов в ОПиУ — как в отчёте «Финансов».
EXPENSE_BLOCKS = (ExpenseKind.Block.MATERIALS, ExpenseKind.Block.FIXED, ExpenseKind.Block.VARIABLE)


def _sum(qs, field):
    return qs.aggregate(v=Coalesce(Sum(field), ZERO, output_field=DecimalField()))["v"]


def months_of(year: int):
    """[(месяц, первый день, последний день), …] календарного года."""
    out = []
    for m in range(1, 13):
        last = calendar.monthrange(year, m)[1]
        out.append((m, date(year, m, 1), date(year, m, last)))
    return out


# --- ОПиУ ----------------------------------------------------------------------


def _lines_money(d_from, d_to, **flt):
    """Выручка части строк за период — тем же правилом, что выручка целиком:
    строки заказов периода + возвращённые позже периода − возвращённые в нём."""
    base = TransactionItem.objects.filter(is_returned=False, **flt).exclude(
        receipt__status=Receipt.Status.CANCELLED
    )
    if d_from:
        base = base.filter(receipt__created_at__date__gte=d_from)
    if d_to:
        base = base.filter(receipt__created_at__date__lte=d_to)
    back = reporting.added_back(d_from, d_to).filter(**flt)
    out = reporting.returned_lines(d_from, d_to).filter(**flt)
    money = reporting.money(base) + reporting.money(back) - reporting.money(out)
    cost = reporting.cost(base) + reporting.cost(back) - reporting.cost(out)
    return money, cost


def losses(d_from, d_to):
    """Брак и недостача по себестоимости — (сумма, записей без себестоимости)."""
    qs = InventoryLog.objects.filter(
        type__in=[InventoryLog.Type.ADJUSTMENT, InventoryLog.Type.WRITE_OFF],
        quantity_changed__lt=0,
    )
    if d_from:
        qs = qs.filter(happened_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(happened_at__date__lte=d_to)
    return _sum(qs.filter(cost__isnull=False), "cost"), qs.filter(cost__isnull=True).count()


def pnl(d_from, d_to) -> dict:
    """ОПиУ за один период.

    Выручка разложена на материал, резку и прочие работы; «прочие» считаются
    остатком от итога, чтобы строки всегда складывались в выручку ровно.
    Себестоимость — на материал и расходники услуг по техкартам, так же
    остатком. Расходы — по статьям «Финансов», которые входят в прибыль.
    """
    revenue = reporting.revenue(d_from, d_to)
    material, material_cost = _lines_money(d_from, d_to, type=TransactionItem.Type.MATERIAL)
    cutting, _ = _lines_money(
        d_from, d_to, type=TransactionItem.Type.SERVICE, service__kind="CUTTING"
    )
    cogs = reporting.cogs(d_from, d_to)

    spent = ExpenseEntry.objects.all()
    if d_from:
        spent = spent.filter(spent_at__gte=d_from)
    if d_to:
        spent = spent.filter(spent_at__lte=d_to)
    by_kind = defaultdict(lambda: ZERO)
    for row in spent.values("kind_id").annotate(v=Sum("amount")):
        by_kind[row["kind_id"]] = row["v"] or ZERO

    loss, loss_unknown = losses(d_from, d_to)
    return {
        "revenue": revenue,
        "revenue_material": material,
        "revenue_cutting": cutting,
        "revenue_other": revenue - material - cutting,
        "cogs": cogs,
        "cogs_material": material_cost,
        "cogs_services": cogs - material_cost,
        "by_kind": dict(by_kind),
        "losses": loss,
        "losses_unknown": loss_unknown,
    }


def _kinds_for(block, values_by_kind):
    """Статьи блока для строк отчёта: все действующие + скрытые, по которым в
    году были траты (иначе скрытие стирало бы расход прошлых месяцев)."""
    kinds = ExpenseKind.objects.filter(block=block).order_by("position", "id")
    return [k for k in kinds if not k.is_archived or any(values_by_kind.get(k.id, []))]


def pnl_year(year: int) -> dict:
    months = months_of(year)
    today = timezone.localdate()
    per_month = [pnl(first, last) for _, first, last in months]

    def col(fn):
        return [fn(p) for p in per_month]

    rows = []

    def add(key, label, values, kind="row", level=1):
        rows.append({
            "key": key, "label": label, "kind": kind, "level": level,
            "values": values,
            "total": sum(values, ZERO) if kind != "percent" else None,
        })

    kind_values = defaultdict(list)
    for p in per_month:
        for kid, v in p["by_kind"].items():
            kind_values[kid].append(v)

    revenue = col(lambda p: p["revenue"])
    add("revenue", "Выручка", revenue, kind="total", level=0)
    add("revenue_material", "Материал", col(lambda p: p["revenue_material"]))
    add("revenue_cutting", "Резка (ЧПУ и лазер)", col(lambda p: p["revenue_cutting"]))
    add("revenue_other", "Прочие работы и услуги", col(lambda p: p["revenue_other"]))

    cogs = col(lambda p: -p["cogs"])
    add("cogs", "Себестоимость проданного", cogs, kind="subtotal", level=0)
    add("cogs_material", "Материал", col(lambda p: -p["cogs_material"]))
    add("cogs_services", "Расходники услуг (по техкартам)", col(lambda p: -p["cogs_services"]))

    gross = [r + c for r, c in zip(revenue, cogs)]
    add("gross", "Валовая прибыль (прибыль до расходов)", gross, kind="total", level=0)
    add("gross_pct", "Валовая маржа, %", [_pct(g, r) for g, r in zip(gross, revenue)],
        kind="percent")

    expenses_total = [ZERO] * 12
    expense_rows = []
    for block in EXPENSE_BLOCKS:
        for k in _kinds_for(block, kind_values):
            if not k.in_profit:
                continue
            values = [-p["by_kind"].get(k.id, ZERO) for p in per_month]
            expense_rows.append((k, values))
            expenses_total = [a + b for a, b in zip(expenses_total, values)]
    add("expenses", "Операционные расходы", expenses_total, kind="subtotal", level=0)
    # Внутри — блоки «Финансов» подытогом и их статьи.
    block_names = dict(ExpenseKind.Block.choices)
    for block in EXPENSE_BLOCKS:
        mine = [(k, v) for k, v in expense_rows if k.block == block]
        if not mine:
            continue
        block_total = [sum(col_, ZERO) for col_ in zip(*(v for _, v in mine))]
        add(f"block:{block}", str(block_names[block]), block_total, kind="group", level=1)
        for k, values in mine:
            add(f"kind:{k.id}", k.name, values, level=2)

    loss = col(lambda p: -p["losses"])
    add("losses", "Списание материала (брак, недостача)", loss, kind="subtotal", level=0)

    profit = [g + e + lo for g, e, lo in zip(gross, expenses_total, loss)]
    add("profit", "Чистая прибыль", profit, kind="grand", level=0)
    add("profit_pct", "Рентабельность, %", [_pct(pr, r) for pr, r in zip(profit, revenue)],
        kind="percent")

    # Справочно — в прибыль не входит: вложения в станки и цех.
    invest_kinds = _kinds_for(ExpenseKind.Block.INVESTMENT, kind_values)
    invest = [sum((p["by_kind"].get(k.id, ZERO) for k in invest_kinds), ZERO) for p in per_month]
    add("investments", "Справочно: инвестиции (в прибыль не входят)", invest, kind="note", level=0)

    # Проценты итоговой колонки — от итогов года, а не суммой месяцев.
    for r in rows:
        if r["key"] == "gross_pct":
            r["total"] = _pct(sum(gross, ZERO), sum(revenue, ZERO))
        elif r["key"] == "profit_pct":
            r["total"] = _pct(sum(profit, ZERO), sum(revenue, ZERO))

    return {
        "year": year,
        "months": [
            {"month": m, "from": first, "to": last, "future": first > today}
            for m, first, last in months
        ],
        "rows": rows,
        "losses_unknown": sum((p["losses_unknown"] for p in per_month), 0),
    }


def _pct(part, whole):
    if not whole:
        return None
    return (Decimal(part) * 100 / Decimal(whole)).quantize(Decimal("0.1"))


# --- ОДДС ----------------------------------------------------------------------

OPERATING, INVESTING, FINANCING, OTHER = "operating", "investing", "financing", "other"

SECTIONS = OrderedDict([
    (OPERATING, "Операционная деятельность"),
    (INVESTING, "Инвестиционная деятельность"),
    (FINANCING, "Финансовая деятельность"),
    (OTHER, "Прочие движения (не деятельность)"),
])

A = CashEntry.Article
# Статья кассы без привязки к трате → (раздел, ключ строки, подпись, порядок).
_BY_ARTICLE = {
    # Оплата, сдача и откат — одни и те же деньги клиента: принёс, получил
    # сдачу, откатили ошибочную оплату. Отдельными строками «сдача» читалась бы
    # как расход цеха, поэтому здесь они в одной строке, нетто.
    A.SALE: (OPERATING, "clients", "Поступления от клиентов", 10),
    A.CHANGE: (OPERATING, "clients", "Поступления от клиентов", 10),
    A.UNPAY: (OPERATING, "clients", "Поступления от клиентов", 10),
    A.REFUND: (OPERATING, "refunds", "Возвраты клиентам", 20),
    A.SUPPLY: (OPERATING, "suppliers", "Оплата поставщикам за материал", 30),
    A.SALARY: (OPERATING, "salary_manual", "Зарплата (внесена в кассе)", 90),
    A.EXPENSE: (OPERATING, "expense_manual", "Расходы цеха (внесены в кассе)", 91),
    A.DEPOSIT: (FINANCING, "owner_in", "Вложения владельца", 10),
    A.OWNER_OUT: (FINANCING, "owner_out", "Изъятия владельца", 20),
    A.LOAN_IN: (FINANCING, "loan_in", "Получено займов", 30),
    A.LOAN_OUT: (FINANCING, "loan_out", "Погашено займов", 40),
    A.TRANSFER: (OTHER, "transfer", "Переводы между кассой и банком", 10),
    A.COUNT: (OTHER, "count", "Пересчёт кассы (излишки и недостачи)", 20),
    A.OTHER: (OTHER, "other", "Прочее", 30),
}

_BLOCK_ORDER = {
    ExpenseKind.Block.FIXED: 40,
    ExpenseKind.Block.MATERIALS: 50,
    ExpenseKind.Block.VARIABLE: 60,
    ExpenseKind.Block.INVESTMENT: 10,
}


def classify(entry):
    """Куда в ОДДС ложится запись кассы: (раздел, ключ, подпись, порядок).

    Трата «Финансов» — по своей статье: аренда, зарплата, коммуналка каждая
    своей строкой, станок и ремонт цеха — в инвестиции. Остальное — по статье
    кассы.
    """
    if entry.expense_id:
        kind = entry.expense.kind
        section = INVESTING if kind.block == ExpenseKind.Block.INVESTMENT else OPERATING
        return section, f"kind:{kind.id}", kind.name, _BLOCK_ORDER.get(kind.block, 70) + kind.position / 1000
    section, key, label, order = _BY_ARTICLE.get(entry.article, (OTHER, "other", "Прочее", 30))
    return section, key, label, order


def cash_flow_year(year: int) -> dict:
    months = months_of(year)
    today = timezone.localdate()
    first_day, last_day = months[0][1], months[-1][2]

    # (раздел, ключ) → {подпись, порядок, значения по месяцам}
    lines = OrderedDict()
    entries = (
        CashEntry.objects.filter(happened_on__gte=first_day, happened_on__lte=last_day)
        .select_related("expense__kind")
    )
    for e in entries:
        section, key, label, order = classify(e)
        slot = lines.setdefault(
            (section, key), {"label": label, "order": order, "values": [ZERO] * 12}
        )
        slot["values"][e.happened_on.month - 1] += e.signed_amount

    # Строки, которые владелец ждёт увидеть всегда — даже пустыми.
    for article in (A.SALE, A.REFUND, A.SUPPLY):
        section, key, label, order = _BY_ARTICLE[article]
        lines.setdefault((section, key), {"label": label, "order": order, "values": [ZERO] * 12})

    opening = [CashEntry.balance(upto=first - timedelta(days=1)) for _, first, _ in months]
    closing = [CashEntry.balance(upto=last) for _, _, last in months]
    by_account = {
        acc: [CashEntry.balance(acc, upto=last) for _, _, last in months]
        for acc in CashEntry.Account.values
    }

    rows = []

    def add(key, label, values, kind="row", level=1, total=None):
        rows.append({
            "key": key, "label": label, "kind": kind, "level": level, "values": values,
            "total": sum(values, ZERO) if total is None else total,
        })

    add("opening", "Остаток на начало", opening, kind="balance", level=0, total=opening[0])
    net = [ZERO] * 12
    for section, title in SECTIONS.items():
        mine = sorted(
            ((key, slot) for (sec, key), slot in lines.items() if sec == section),
            key=lambda kv: (kv[1]["order"], kv[1]["label"]),
        )
        if not mine and section != OPERATING:
            continue
        subtotal = [ZERO] * 12
        for _, slot in mine:
            subtotal = [a + b for a, b in zip(subtotal, slot["values"])]
        add(f"section:{section}", title, subtotal, kind="subtotal", level=0)
        for key, slot in mine:
            add(f"{section}:{key}", slot["label"], slot["values"])
        net = [a + b for a, b in zip(net, subtotal)]
    add("net", "Чистый денежный поток", net, kind="total", level=0)
    add("closing", "Остаток на конец", closing, kind="grand", level=0, total=closing[-1])
    labels = dict(CashEntry.Account.choices)
    for acc, values in by_account.items():
        add(f"closing:{acc}", f"в т.ч. {str(labels[acc]).lower()}", values, level=1,
            total=values[-1])

    return {
        "year": year,
        "months": [
            {"month": m, "from": first, "to": last, "future": first > today}
            for m, first, last in months
        ],
        "rows": rows,
    }
