"""Выгрузка периода для бухгалтера (G2-N1): ОПиУ, ОДДС и сверка в один CSV.

Квартал или любой отрезок одним файлом: ОПиУ с основой налога, ОДДС с разрезом
«наличные / безнал / всего» и сверка «прибыль → деньги». Числа — те же функции,
что на экранах (`pnl`, `cash_flow`, `bridge`), поэтому файл сходится с ними до
тыйына.
"""
from __future__ import annotations

from sales import reporting
from sales.models import Receipt

from .. import chart
from ..models import CashEntry, TaxRate
from .bridge import bridge
from .cashflow import cash_flow
from .money import ZERO
from .pnl import pnl, resolve

CASH, BANK = CashEntry.Account.CASH, CashEntry.Account.BANK
BASIS = {
    TaxRate.Basis.ACCRUAL: "по начислению (от выручки)",
    TaxRate.Basis.CASH: "по кассе (от полученных денег)",
    "MIXED": "менялась внутри периода",
}


def period_rows(d_from, d_to) -> list[list]:
    d_from, d_to = resolve(d_from, d_to)
    p = pnl(d_from, d_to)
    cf = cash_flow(d_from, d_to)
    br = bridge(d_from, d_to)

    cash_receipts = Receipt.objects.filter(payment_method="CASH")
    revenue_cash = reporting.revenue(d_from, d_to, receipts=cash_receipts)

    rows: list[list] = [
        ["Отчёт за период", d_from, d_to],
        ["Основа налога", BASIS.get(p["tax_basis"], p["tax_basis"])],
        [],
        ["ОПиУ", "Сумма, сом"],
        ["Выручка", p["revenue"]],
        ["  в том числе материал", p["revenue_material"]],
        ["  в том числе резка", p["revenue_cutting"]],
        ["  в том числе прочие работы и услуги", p["revenue_other"]],
        ["  выручка наличными", revenue_cash],
        ["  выручка безналом", p["revenue"] - revenue_cash],
        ["Себестоимость материала", -p["cogs_material"]],
        ["Расходники услуг", -p["cogs_services"]],
        ["Гарантийные переделки", -p["cogs_warranty"]],
        ["Потери материала (брак, недостача)", -p["losses"]],
        ["Валовая прибыль", p["gross_profit"]],
        ["Операционные расходы", -p["opex"]["total"]],
    ]
    for block in p["opex"]["blocks"]:
        rows.append([f"  {block['label']}", -block["total"]])
        for r in block["rows"]:
            rows.append([f"    {r['name']}", -r["amount"]])
    if p["opex_cash_manual"]:
        rows.append(["Расходы, внесённые прямо в кассе", -p["opex_cash_manual"]])
    if p["cash_count"]:
        rows.append(["Недостача / излишек кассы", p["cash_count"]])
    rows += [
        ["EBITDA", p["ebitda"]],
        ["Амортизация", -(p["depreciation"] + p["disposal"])],
        ["Операционная прибыль", p["operating_profit"]],
        ["Проценты по займам", -p["interest"]],
        [p["tax_label"], -p["tax"]],
        ["Чистая прибыль", p["net_profit"]],
        [],
        ["ОДДС", "Наличные", "Безнал", "Всего"],
        ["Остаток на начало", cf["by_account"][CASH]["opening"], cf["by_account"][BANK]["opening"], cf["opening"]],
    ]
    for section in chart.FLOW_SECTIONS:
        data = cf["sections"][section]
        if not data["lines"] and section != chart.OPERATING:
            continue
        rows.append([data["label"], data["by_account"][CASH], data["by_account"][BANK], data["total"]])
        for line in data["lines"]:
            rows.append([f"  {line['label']}", line["by_account"][CASH], line["by_account"][BANK], line["amount"]])
    rows.append([
        "Чистый денежный поток",
        sum((cf["sections"][s]["by_account"][CASH] for s in chart.FLOW_SECTIONS), ZERO),
        sum((cf["sections"][s]["by_account"][BANK] for s in chart.FLOW_SECTIONS), ZERO),
        cf["net_flow"],
    ])
    outside = cf["sections"][chart.OUTSIDE]
    if outside["lines"]:
        rows.append([outside["label"], outside["by_account"][CASH], outside["by_account"][BANK], outside["total"]])
        for line in outside["lines"]:
            rows.append([f"  {line['label']}", line["by_account"][CASH], line["by_account"][BANK], line["amount"]])
    rows += [
        ["Остаток на конец", cf["by_account"][CASH]["closing"], cf["by_account"][BANK]["closing"], cf["closing"]],
        [],
        ["Сверка: почему прибыль не равна деньгам", "Сумма, сом"],
    ]
    for line in br["lines"]:
        rows.append([line["label"], line["amount"]])
    rows.append(["Чистый денежный поток (как в ОДДС)", br["net_cash_flow"]])
    return rows
