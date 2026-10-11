"""Сверка «чистая прибыль → чистый денежный поток» — почему прибыль ≠ деньгам.

Каждая строка — изменение за период, посчитанное из тех же записей, что ОПиУ и
ОДДС, поэтому строки складываются ровно:

    Чистая прибыль
  + Амортизация и списание выбывшего          деньги за них ушли раньше, при покупке
  − Рост долга клиентов                        выручка есть, денег ещё нет
  + Рост сдачи и предоплат клиентов            деньги есть, а выручки нет (D-11)
  − Рост запасов на складе                     пришло − продано − списано
  + Рост долга поставщикам                     материал пришёл, не оплачен
  (в отчёте строки названы нейтрально — «Долг клиентов», «Запасы на складе», —
  а знак значит влияние на деньги)
  + Расходы начислены, но не оплачены          «за сентябрь» оплачено в октябре (D-2);
                                               сюда же — зарплата, начисленная по
                                               ведомости, но ещё не выплаченная
  + Списанные долги клиентов                   расход без денег (BAD_DEBT)
  ± Входящие остатки клиентов                  деньги за долг, висевший до переезда из
                                               Excel, минус траты аванса, принятого до
                                               переезда (волна 2)
  + Налог начислен, но не уплачен
  − Покупка оборудования и цеха                деньги ушли целиком, в прибыль — частями
  ± Финансовая деятельность                    владелец и займы
  ± Прочие движения кассы                      статья «Прочее» (D-29)
  ± Не объяснено                               должно быть 0
  = Чистый денежный поток (как в ОДДС)

«Не объяснено» по построению — ноль: строки выведены из тех же денег. Ненулевое
значение значит, что сами данные спорят друг с другом (например, итог чека не
равен сумме его строк), — это сигнал искать ошибку, а не строка отчёта.

Долг клиентов и их деньги у нас считаются по каждому клиенту: выручка по его
чекам минус его деньги по кассовой книге. Плюс — он должен (долг), минус — у
нас лежат его деньги (сдача, предоплата неоплаченного онлайн-заказа). Чек без
клиента — сам себе «клиент»; деньги удалённых заказов — отдельной группой.
Зачёт сдачи в новый заказ денег не двигает и внутри клиента сам себя гасит.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from django.utils import timezone

from sales.models import Receipt, TransactionItem

from .. import chart
from ..material_sheet import purchases_from_stock_by_day
from ..models import CashEntry, ExpenseKind
from ..periods import local_day, month_end
from .cashflow import cash_flow, entries
from .money import ZERO, q2, total
from .pnl import pnl, resolve
from .quarters import add_quarters, quarter_meta
from .scope import once, report_scope

A = CashEntry.Article
CLIENT_ARTICLES = (A.SALE, A.CHANGE, A.UNPAY, A.REFUND)

# Знак строки — влияние на деньги: плюс — денег стало больше, чем прибыли,
# минус — меньше. Поэтому названия нейтральные («Долг клиентов»), а не «рост
# долга»: рост долга клиентов — минус, его погашение — плюс.
LABELS = {
    "net_profit": "Чистая прибыль",
    "non_cash": "Амортизация (деньги ушли раньше, при покупке)",
    "receivables": "Долг клиентов",
    "client_money": "Сдача и предоплаты клиентов",
    "inventory": "Запасы на складе",
    "payables": "Долг поставщикам",
    "accrued": "Расходы начислены, но не оплачены",
    "written_off": "Списанные долги клиентов (деньги не двигались)",
    "opening_balances": "Входящие остатки клиентов (долг и аванс до переезда)",
    "tax_payable": "Налог начислен, но не уплачен",
    "capex": "Покупка оборудования и цеха",
    "financing": "Деньги владельца и займы",
    "other": "Прочие движения кассы (статья «Прочее»)",
    "unexplained": "Не объяснено",
    "net_cash_flow": "Чистый денежный поток",
}


def client_positions(days: list[date]) -> dict:
    """{день: (долг клиентов, их деньги у нас)} на конец каждого дня."""
    sold, returned, cash = once("client_ledger", _client_ledger)
    out = {}
    for day in days:
        position = defaultdict(lambda: ZERO)
        for group, recognized, value in sold:
            if recognized <= day:
                position[group] += value
        for group, recognized, back_on, value in returned:
            if recognized <= day < back_on:
                position[group] += value
        for group, paid_on, value in cash:
            if paid_on <= day:
                position[group] -= value
        out[day] = (
            total(v for v in position.values() if v > 0),
            total(-v for v in position.values() if v < 0),
        )
    return out


def _client_ledger():
    """Продажи, поздние возвраты и деньги клиентов по группам («клиент» или
    «чек без клиента»). Читается один раз на отчёт: годовая сверка спрашивала
    эти таблицы заново для каждого месяца."""
    group_of = {}
    for rid, client_id in Receipt.objects.values_list("id", "client_id"):
        group_of[rid] = f"c{client_id}" if client_id else f"r{rid}"

    sold = []    # (группа, день признания, итог − возвращено)
    for rid, recognized, total_price, refunded in (
        Receipt.objects.exclude(status=Receipt.Status.CANCELLED)
        .filter(revenue_recognized_at__isnull=False)
        .values_list("id", "revenue_recognized_at", "total_price", "refunded_amount")
    ):
        sold.append((group_of[rid], local_day(recognized), total_price - refunded))
    # Строки, вернувшиеся ПОЗЖЕ дня: в этот день они ещё были продажей. Целиком
    # возвращённый заказ (статус «Отменён») здесь тоже нужен: из `sold` он
    # исключён совсем, а в месяце продажи он был и выручкой, и деньгами —
    # без этих строк сентябрь показывал «не объяснено −3 000», октябрь +3 000.
    returned = [
        (group_of[it.receipt_id], local_day(it.receipt.revenue_recognized_at),
         local_day(it.returned_at), it.sold_total)
        for it in TransactionItem.objects.filter(
            is_returned=True, returned_at__isnull=False,
            receipt__revenue_recognized_at__isnull=False,
        )
        .select_related("receipt").only("quantity", "price_per_item", "returned_at",
                                        "receipt__revenue_recognized_at", "receipt_id")
    ]
    # Оплаты входящего долга (волна 2) — не долг и не деньги клиентов системы:
    # они идут строкой «Входящие остатки» (`opening_flows`), здесь их нет.
    from clients.opening import opening_advance_uses, opening_cash_entry_ids

    opening_cash = opening_cash_entry_ids()
    cash = [
        (group_of.get(rid, "deleted") if rid else "deleted", day,
         amount if kind == CashEntry.Kind.IN else -amount)
        for pk, rid, day, kind, amount in CashEntry.objects.filter(
            article__in=CLIENT_ARTICLES
        ).values_list("pk", "receipt_id", "happened_on", "kind", "amount")
        if pk not in opening_cash
    ]
    # Траты аванса, принятого ДО переезда: заказ оплачен деньгами, которых в
    # кассе системы нет, — для клиента это оплата, а в сверке строка «Входящие
    # остатки» (минусом).
    for client_id, used_on, amount in opening_advance_uses():
        cash.append((f"c{client_id}", used_on, amount))
    # Аванс клиента (D-93) пишется в кассу без чека и попадал в общую группу
    # «без клиента»: долг и деньги клиентов в подсказке сверки завышались на
    # потраченный аванс. Переносим его в группу клиента (волна 2) — парой
    # «минус там, плюс здесь» того же дня, поэтому сумма позиций, а с ней и
    # «Не объяснено», не меняется.
    from clients.models import ClientAdvance

    for client_id, paid_on, amount, reverted_at in ClientAdvance.objects.filter(
        is_opening=False,       # входящий аванс кассовой записи не имеет
    ).values_list("client_id", "paid_on", "amount", "reverted_at"):
        cash.append((f"c{client_id}", paid_on, amount))
        cash.append(("deleted", paid_on, -amount))
        if reverted_at:
            back_on = local_day(reverted_at)
            cash.append((f"c{client_id}", back_on, -amount))
            cash.append(("deleted", back_on, amount))

    return sold, returned, cash


# --- Долг поставщикам: сальдо на дату (D-195) -----------------------------------------
#
# Строка «Долг поставщикам» — изменение сальдо расчётов с поставщиками за
# период. Сальдо на конец дня — из тех же записей, что сама строка:
#
#     начальные долги (по дату) + закуп (по дату) − оплаты поставщикам (по дату)
#     + курсовой доход по оплатам (закрыл долг без денег)
#
# и на сегодня оно равно карточке «Долг поставщикам» минус «Авансы
# поставщикам» (`summary.supplier_debts` / `supplier_advances`): приход без
# указанной оплаты — долг и там, и здесь. Поэтому
#
#     строка за период = сальдо на конец − сальдо на начало
#                        − начальные долги, внесённые в периоде
#
# Начальный долг — не движение денег и не закуп, в строку он не входит.


def _is_supplier_payment(entry, mapping) -> bool:
    """Запись кассы гасит долг поставщикам: «Оплата поставщику» или трата вида
    «Закуп в склад» — в денежном потоке."""
    if mapping.cash_section not in chart.FLOW_SECTIONS:
        return False
    return entry.article == A.SUPPLY or bool(
        entry.expense_id and entry.expense.kind.role == ExpenseKind.Role.INVENTORY
    )


def _purchases_by_day():
    return once("purchases_by_day", purchases_from_stock_by_day)


def _purchases_upto(day):
    """Закуп по день включительно — до тыйына."""
    return q2(sum((v for d, v in _purchases_by_day().items() if d <= day), ZERO))


def _supplier_cash():
    """[(день, сколько ушло поставщикам)] по всей книге — один раз на отчёт."""
    def load():
        from django.db.models import Q

        out = []
        for e in CashEntry.objects.filter(
            Q(article=A.SUPPLY) | Q(expense__kind__role=ExpenseKind.Role.INVENTORY)
        ).select_related("expense__kind"):
            if _is_supplier_payment(e, chart.for_cash_entry(e)):
                out.append((e.happened_on, -e.signed_amount))
        return out

    return once("supplier_cash", load)


def _fx_settled():
    """[(день, сумма)] курсовой разницы БЕЗ денег — доход по оплате в валюте
    (заплатили меньше, чем закрыли долга) и его отмена. Карточка закрывает
    долг на всю закрытую сумму, касса — только на деньги: разницу сверка
    переносит из «Расходы начислены, но не оплачены» в «Долг поставщикам»."""
    def load():
        # Траты — из той же загрузки, что ОПиУ (таблица трат читается один раз
        # на отчёт); у каких есть деньги — одним запросом к книге.
        from .pnl import _load_expenses

        fx = [e for e in once("expense_entries", _load_expenses) if e.kind.code == "FX_DIFF"]
        if not fx:
            return []
        with_cash = set(
            CashEntry.objects.filter(expense_id__in=[e.id for e in fx]).values_list("expense_id", flat=True)
        )
        return [(e.spent_at, e.amount) for e in fx if e.id not in with_cash]

    return once("fx_settled", load)


def _openings():
    def load():
        from warehouse.models import SupplierOpeningDebt

        return list(SupplierOpeningDebt.objects.values_list("as_of", "amount"))

    return once("supplier_openings", load)


def _sum_between(rows, d_from, d_to):
    return total(v for d, v in rows if (d_from is None or d >= d_from) and d <= d_to)


def supplier_position(day):
    """Сальдо расчётов с поставщиками на конец дня `day`: долги минус авансы.
    На сегодня — карточка «Долг поставщикам» минус «Авансы поставщикам»."""
    return (
        _sum_between(_openings(), None, day) + _purchases_upto(day)
        - _sum_between(_supplier_cash(), None, day) + _sum_between(_fx_settled(), None, day)
    )


def opening_flows(d_from, d_to):
    """Строка «Входящие остатки» за период: деньги, принесённые за входящий долг
    (приходы минус откаты по их записям кассы), минус траты входящего аванса."""
    from clients.opening import opening_advance_uses, opening_cash_entry_ids

    value = ZERO
    ids = opening_cash_entry_ids()
    if ids:
        for kind, amount in CashEntry.objects.filter(
            pk__in=ids, happened_on__gte=d_from, happened_on__lte=d_to,
        ).values_list("kind", "amount"):
            value += amount if kind == CashEntry.Kind.IN else -amount
    for _client, used_on, amount in opening_advance_uses():
        if d_from <= used_on <= d_to:
            value -= amount
    return value


@report_scope
def bridge(d_from=None, d_to=None) -> dict:
    """Сверка за период: строки, итог и «Не объяснено»."""
    d_from, d_to = resolve(d_from, d_to)
    p = pnl(d_from, d_to)
    cf = cash_flow(d_from, d_to)
    start = d_from - timedelta(days=1)
    positions = client_positions([start, d_to])
    (ar0, held0), (ar1, held1) = positions[start], positions[d_to]

    # Закуп по цене за единицу даёт хвосты мельче тыйына — до тыйына, как
    # все деньги отчёта (`money.q2`); строки запасов и долга поставщикам он
    # двигает одинаково, и их сумма от округления не меняется. Закуп периода —
    # разница закупа «по день» на концах периода: тем же числом двигается
    # сальдо поставщиков (`supplier_position`), без расхождения в тыйын.
    purchases = _purchases_upto(d_to) - _purchases_upto(start)
    supplier_paid = ZERO
    expenses_paid = ZERO
    tax_paid = ZERO
    other = ZERO
    for e in entries(d_from, d_to):
        mapping = chart.for_cash_entry(e)
        if mapping.cash_section not in chart.FLOW_SECTIONS:
            continue
        if _is_supplier_payment(e, mapping):
            supplier_paid -= e.signed_amount
        elif e.article == A.PAYROLL and not e.expense_id:
            expenses_paid -= e.signed_amount        # выплата по ведомости гасит начисление
        elif e.expense_id and mapping.pnl in (chart.OPEX, chart.INTEREST):
            expenses_paid -= e.signed_amount
        elif e.expense_id and e.expense.kind.role == ExpenseKind.Role.TAX:
            tax_paid -= e.signed_amount
        elif not e.expense_id and e.article == A.OTHER:
            other += e.signed_amount
    # Курсовой доход по оплате в валюте закрыл долг без денег (D-195): в долг
    # поставщикам, а не в «начислено, но не оплачено» — как в карточке.
    fx_settled = _sum_between(_fx_settled(), d_from, d_to)
    opening_debts = _sum_between(_openings(), d_from, d_to)

    lines = [
        ("net_profit", p["net_profit"]),
        ("non_cash", p["depreciation"] + p["disposal"]),
        ("receivables", -(ar1 - ar0)),
        ("client_money", held1 - held0),
        ("inventory", -(purchases - p["cogs_total"])),
        ("payables", purchases - supplier_paid + fx_settled),
        # Списание безнадёжного долга — расход, у которого денег не было и не
        # будет: он не «ждёт оплаты», а гасит долг клиента (отдельной строкой).
        ("accrued", p["opex"]["total"] + p["interest"] - expenses_paid - p["opex_noncash"] - fx_settled),
        ("written_off", p["opex_noncash"]),
        ("opening_balances", opening_flows(d_from, d_to)),
        ("tax_payable", p["tax"] - tax_paid),
        ("capex", cf["sections"][chart.INVESTING]["total"]),
        ("financing", cf["sections"][chart.FINANCING]["total"]),
        ("other", other),
    ]
    explained = total(v for _, v in lines)
    unexplained = cf["net_flow"] - explained
    lines.append(("unexplained", unexplained))
    return {
        "period": {"from": d_from, "to": d_to},
        "lines": [{"key": k, "label": LABELS[k], "amount": v} for k, v in lines],
        "net_cash_flow": cf["net_flow"],
        "unexplained": unexplained,
        # Уровни на конец периода — подсказкам к строкам.
        "levels": {
            "receivables": ar1, "client_money": held1,
            "receivables_start": ar0, "client_money_start": held0,
            # Сальдо с поставщиками на концах периода и начальные долги,
            # внесённые в периоде (D-195): строка «Долг поставщикам» =
            # конец − начало − начальные долги.
            "payables_start": supplier_position(start), "payables_end": supplier_position(d_to),
            "payables_opening": opening_debts,
            "payables_start_on": start, "payables_end_on": d_to,
        },
    }


@report_scope
def bridge_year(year: int) -> dict:
    today = timezone.localdate()
    months = [(date(year, m, 1), month_end(date(year, m, 1))) for m in range(1, 13)]
    per = [bridge(first, last) for first, last in months]
    keys = [line["key"] for line in per[0]["lines"]]
    rows = []
    for i, key in enumerate(keys):
        values = [b["lines"][i]["amount"] for b in per]
        kind = "total" if key == "net_profit" else ("warn" if key == "unexplained" else "row")
        if key in ("written_off", "opening_balances") and not any(values):
            continue                      # редкая строка: пустой год её не показывает
        rows.append({
            "key": key, "label": LABELS[key], "kind": kind, "level": 0 if key == "net_profit" else 1,
            "values": values, "total": total(values), "hint": f"bridge_{key}",
        })
    flow = [b["net_cash_flow"] for b in per]
    rows.append({
        "key": "net_cash_flow", "label": LABELS["net_cash_flow"], "kind": "grand", "level": 0,
        "values": flow, "total": total(flow), "hint": "net_cash_flow",
    })
    month_info = [
        {"month": first.month, "from": first, "to": last, "future": first > today}
        for first, last in months
    ]
    add_quarters(rows)
    return {
        "year": year,
        "months": month_info,
        "quarters": quarter_meta(month_info),
        "rows": rows,
    }
