"""«Обзор» — главное на одном экране (2026-10-07, этап 2).

Четыре плитки — выручка, чистая прибыль с маржой, чистый денежный поток, деньги
на конец периода — у каждой период, валюта и изменение к прошлому периоду, а
под ними короткое «почему прибыль ≠ деньгам» из сверки (`headline`). Ниже —
разборы прежнего «Обзора» (`dashboard`): выручка по способам оплаты, материал,
списания, склад, материалы на исходе.

Денежные цифры — из тех же функций, что «Финансы»: выручка и прибыль из ОПиУ
(`pnl.pnl`), деньги из ОДДС (`cashflow.cash_flow`), «почему» — из сверки
(`bridge.bridge`). «Получено» берётся из кассовой книги, а не из заказов.

`dashboard` перенесён из `audit.views.DashboardView`: обработчик теперь только
читает период и отдаёт результат.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce

from sales import reporting
from sales.models import Receipt, TransactionItem
from warehouse.models import Material, stock_value_total

from ..periods import add_months, month_end, month_start
from .bridge import bridge
from .cashflow import cash_flow
from .money import pct
from .pnl import losses_qs, pnl


def _line_sum(items) -> Decimal:
    """Выручка строк — вверх до целого сома, как `TransactionItem.line_total`
    и итог чека. Считаем в Python по Decimal, а не CEIL в базе: SQLite
    умножает в double и даёт копеечный шум."""
    return sum((it.line_total for it in items.only("quantity", "price_per_item", "is_returned")), Decimal("0"))


# Себестоимость строк — снимок закупки на момент списания со склада.
_COST_SUM = Coalesce(Sum("cost_total"), Decimal("0"), output_field=DecimalField())


# --- Главные плитки ---------------------------------------------------------------


def previous_period(d_from, d_to):
    """Прошлый период для сравнения. Месяц — прошлый месяц; произвольный
    отрезок — такой же длины сразу перед ним; «весь период» — сравнивать не с
    чем (None)."""
    if d_from is None or d_to is None:
        return None
    if d_from == month_start(d_from) and d_to == month_end(d_from):
        prev = add_months(d_from, -1)
        return prev, month_end(prev)
    length = (d_to - d_from).days + 1
    return d_from - timedelta(days=length), d_from - timedelta(days=1)


def _change(now, before):
    if before is None:
        return None
    return {"before": before, "delta": now - before, "delta_pct": pct(now - before, abs(before)) if before else None}


def headline(d_from=None, d_to=None) -> dict:
    """Четыре главные цифры периода, сравнение с прошлым и «почему прибыль ≠
    деньгам» (три самые крупные строки сверки, кроме самой прибыли)."""
    p = pnl(d_from, d_to)
    cf = cash_flow(p["period"]["from"] if d_from else None, p["period"]["to"])
    br = bridge(d_from, d_to)
    prev = previous_period(d_from, d_to)
    p0 = cf0 = None
    if prev:
        p0, cf0 = pnl(*prev), cash_flow(*prev)

    def pick(data, key):
        return data[key] if data is not None else None

    reasons = sorted(
        (line for line in br["lines"] if line["key"] not in ("net_profit", "unexplained") and line["amount"]),
        key=lambda line: -abs(line["amount"]),
    )[:3]
    return {
        "period": {"from": p["period"]["from"], "to": p["period"]["to"], "all_time": d_from is None},
        "previous": {"from": prev[0], "to": prev[1]} if prev else None,
        "currency": "сом",
        "revenue": {"value": p["revenue"], "change": _change(p["revenue"], pick(p0, "revenue"))},
        "net_profit": {
            "value": p["net_profit"], "margin": p["margins"]["net"],
            "change": _change(p["net_profit"], pick(p0, "net_profit")),
        },
        "net_cash_flow": {"value": cf["net_flow"], "change": _change(cf["net_flow"], pick(cf0, "net_flow"))},
        "cash_end": {
            "value": cf["closing"],
            "by_account": {acc: v["closing"] for acc, v in cf["by_account"].items()},
            "change": _change(cf["closing"], pick(cf0, "closing")),
        },
        "received": cf["received_from_clients"],
        "why": {
            "net_profit": p["net_profit"],
            "net_cash_flow": cf["net_flow"],
            "reasons": reasons,
            "unexplained": br["unexplained"],
        },
    }


# --- Разборы прежнего «Обзора» --------------------------------------------------------


def dashboard(d_from=None, d_to=None) -> dict:
    """Разборы «Обзора» за период (границы включительные, None — без границы)."""
    # Опциональный период фильтрует денежные показатели по дню продажи (дню
    # признания выручки); складские (актив, материалы на исходе) — на конец
    # периода или «на сейчас».
    p = pnl(d_from, d_to)

    def by_period(qs, field="revenue_recognized_at"):
        # Неоплаченный онлайн-счёт — не продажа: у него даты признания нет.
        if field.endswith("revenue_recognized_at"):
            qs = qs.filter(**{f"{field}__isnull": False})
        if d_from:
            qs = qs.filter(**{f"{field}__date__gte": d_from})
        if d_to:
            qs = qs.filter(**{f"{field}__date__lte": d_to})
        return qs

    # Выручка считается по ВСЕМ заказам периода, кроме отменённых, — по дате
    # заказа. Раньше сюда попадали только полностью оплаченные чеки, и заказ
    # в долг не считался выручкой вообще: материал со склада ушёл, работа
    # сделана, а в отчёте её нет. Отдельно от этого «Финансы» показывают,
    # сколько из выручки уже получено на руки, а сколько ещё в долгу.
    paid = by_period(Receipt.objects.exclude(status=Receipt.Status.CANCELLED))

    # Стоимость склада — по остаткам ПАРТИЙ, у каждой своя себестоимость
    # (Material.stock_value). Раньше здесь стояло quantity × purchase_price,
    # то есть весь остаток оценивался ценой последнего прихода. Считает её
    # `stock_value_total` — та же функция, что у «Склада (оборот)» в
    # «Финансах»: своя формула там разошлась с этой (15.09).
    #
    # ДВА разных набора материалов, и путать их нельзя:
    #  · стоимость склада — что лежит на складе и стоит денег. Скрытый
    #    материал с остатком СЮДА ВХОДИТ: он физически на полке. Раньше он
    #    выпадал целиком, и нажатие «Удалить» на позиции с товаром мгновенно
    #    уменьшало активы — 7 201 сом исчезали одним кликом, хотя материал
    #    никуда не делся. Исходная жалоба («удалил, а он в отчётах») закрыта
    #    другим: материал БЕЗ продаж удаляется насовсем вместе с приходами,
    #    а прячется только тот, по которому продажи были, — настоящий товар.
    #    Опустевший скрытый материал даёт ноль и так.
    #  · `live_materials` — чем цех торгует. Скрытого тут нет: докупать то,
    #    что убрали из каталога, не нужно.
    live_materials = Material.objects.filter(is_archived=False)
    # Склад — НА КОНЕЦ ВЫБРАННОГО ПЕРИОДА, а не «всегда сегодняшний».
    # Плитка стояла среди месячных и показывала сегодняшнюю цифру в любом
    # месяце: в августе, где не было ни одной продажи, она держала
    # 1 184 614. Список «на исходе» ниже остаётся «на сейчас» — это про
    # «что докупить», и вчерашний дефицит там не нужен.
    stock_value = stock_value_total(d_to)

    # Выручка по способам оплаты (нал / MBank / DemirBank / онлайн) — той
    # же формулой, что «Выручка» в Финансах (`sales.reporting`): заказы
    # периода минус возвраты, оформленные в периоде. До общей формулы Обзор
    # показывал 6 231 там, где Финансы — 5 331 (частичный возврат на 900).

    def rev(method):
        return reporting.revenue(
            d_from, d_to, receipts=Receipt.objects.filter(payment_method=method)
        )

    revenue_cash = rev(Receipt.PaymentMethod.CASH)
    revenue_mbank = rev(Receipt.PaymentMethod.MBANK)
    revenue_demirbank = rev(Receipt.PaymentMethod.DEMIRBANK)
    revenue_online = rev(Receipt.PaymentMethod.ONLINE)
    revenue_total = revenue_cash + revenue_mbank + revenue_demirbank + revenue_online

    # СКОЛЬКО ИЗ ЭТОГО УЖЕ ПОЛУЧИЛИ, а сколько ещё должны — тем же
    # разрезом. Без этих двух строк «Наличные 449 042» читаются как деньги
    # в ящике, а это СТОИМОСТЬ ЗАКАЗОВ: на проде 19.09 из них не заплачено
    # 314 141, и в ящике лежало 114 707. Плитка и касса спорили вчетверо,
    # и это первое, что заказчик складывает в уме.
    #
    # Способ у полученных денег берём НЕ у чека, а у самой оплаты: долг
    # часто гасят не тем способом, которым оформляли заказ (наличный заказ
    # закрывают переводом). Первая оплата при оформлении своей записи
    # `Payment` не заводит — это остаток суммы чека сверх записанных
    # погашений, и он идёт способом чека. Ровно так же разносит деньги
    # кассовая книга (`finance.cash.account_for`), поэтому «получено
    # наличными» здесь и наличный остаток кассы — об одном и том же.
    received = defaultdict(lambda: Decimal("0"))
    debt = defaultdict(lambda: Decimal("0"))
    for r in paid.prefetch_related("payments"):
        kept = r.total_price - r.refunded_amount
        # Больше, чем стоит оставшийся заказ, выручкой не считаем: лишнее —
        # это сдача или возвращённые деньги, у них свои поля (та же
        # оговорка, что у `revenue_paid` в «Финансах»).
        cap = min(r.amount_paid, kept) if kept > 0 else Decimal("0")
        rows = []
        payments = sorted(r.payments.all(), key=lambda p: (p.paid_on, p.id))
        first = r.amount_paid - sum((p.amount for p in payments), Decimal("0"))
        if first > 0:
            rows.append((r.payment_method, first))
        rows += [(p.method, p.amount) for p in payments]
        left = cap
        for method, amount in rows:
            if left <= 0:
                break
            take = min(amount, left)
            received[method] += take
            left -= take
        owed = kept - r.amount_paid
        if r.payment_status in Receipt.OWING_STATUSES and owed > 0:
            debt[r.payment_method] += owed

    def split(source):
        """Четыре способа и итог — одной формой, как у выручки выше."""
        out = {
            "cash": source[Receipt.PaymentMethod.CASH],
            "mbank": source[Receipt.PaymentMethod.MBANK],
            "demirbank": source[Receipt.PaymentMethod.DEMIRBANK],
            "online": source[Receipt.PaymentMethod.ONLINE],
        }
        out["total"] = sum(out.values(), Decimal("0"))
        return out

    # Разбивка выручки — работа против материала — тем же правилом, что и
    # выручка выше: строки заказов периода, плюс возвращённые позже периода
    # (тогда они были продажей), минус возвращённые в периоде.
    paid_lines = by_period(
        TransactionItem.objects.filter(is_returned=False).exclude(
            receipt__status=Receipt.Status.CANCELLED
        ),
        field="receipt__revenue_recognized_at",
    )
    back = reporting.added_back(d_from, d_to)
    out = reporting.returned_lines(d_from, d_to)

    def lines_money(kind):
        return (
            _line_sum(paid_lines.filter(type=kind))
            + reporting.money(back.filter(type=kind))
            - reporting.money(out.filter(type=kind))
        )

    def lines_cost(kind=None):
        parts = [paid_lines, back, out]
        if kind:
            parts = [q.filter(type=kind) for q in parts]
        return (
            parts[0].aggregate(v=_COST_SUM)["v"]
            + reporting.cost(parts[1]) - reporting.cost(parts[2])
        )

    work_revenue = lines_money(TransactionItem.Type.SERVICE)
    material_revenue = lines_money(TransactionItem.Type.MATERIAL)
    # Себестоимость проданного материала — по ТЕМ ЖЕ строкам, что и выручка
    # (тот же период, только оплаченные и невозвращённые). Цифра снята в
    # момент списания со склада: для рулонных — по FIFO-партиям, откуда
    # материал реально ушёл. Одна выручка без неё не отвечала на вопрос
    # «сколько на материале заработали»: 149 232 сом продали — а купили их
    # почём?
    material_cost = lines_cost(TransactionItem.Type.MATERIAL)
    # Себестоимость ВСЕГО проданного — из ОПиУ (`cogs_total` в ответе ниже).

    service_items = by_period(
        TransactionItem.objects.filter(
            type=TransactionItem.Type.SERVICE, is_returned=False
        ),
        field="receipt__revenue_recognized_at",
    )
    services_count = service_items.count()

    # Material consumed by services, via technological cards — той же
    # формулой, что списывает склад: «на кв.м» от площади куска, «фикс» раз
    # на строку (у резки `quantity` — погонные метры, не площадь).
    from sales.sale_service import recipe_consumption

    materials_consumed = Decimal("0")
    for item in service_items.select_related("service").prefetch_related(
        "service__recipes"
    ):
        for recipe in item.service.recipes.all():
            materials_consumed += recipe_consumption(recipe, item)

    # Возвраты, ОФОРМЛЕННЫЕ в периоде, — по дате возврата, как в выручке.
    refunded_total = reporting.refunds(d_from, d_to)

    # СПИСАНО МИМО ПРОДАЖИ — недостача по инвентаризации и брак.
    #
    # Главное здесь ДЕНЬГИ: материал, вынесенный со склада правкой остатка,
    # не попадает ни в себестоимость, ни в расходы, и склад просто худеет.
    # На проде 19.09 так ушло 103 424 сома — и не было экрана, где эта
    # сумма появлялась хоть раз. Себестоимость движения знает журнал: у
    # площадных — по партиям, у штучных — по закупочной; у записей до
    # 04.09 её нет, поэтому «сколько» и «на сколько» — разные ответы.
    #
    # Количество складывать по материалам НЕЛЬЗЯ (штуки диодов с
    # квадратными метрами акрила), поэтому наружу отдаём только деньги и
    # число записей. Период — тот же, что у остальных денежных плиток:
    # раньше эта цифра считалась по всей истории и с ними не дружила.
    # Те же записи и та же сумма, что строка «Потери материала» ОПиУ.
    lost_rows = losses_qs(d_from, d_to).count()
    lost_cost, lost_unknown = p["losses"], p["losses_unknown"]

    # Виды материалов на исходе (остаток ≤ критического) — список, не только
    # счёт. Скрытые не показываем: докупать то, что удалили из каталога, не
    # нужно.
    low_stock_items = [
        {
            "id": m.id,
            "name": m.name,
            "quantity": m.quantity,
            "unit": m.unit,
            "critical_balance": m.critical_balance,
            "sheets_remaining": m.sheets_remaining,
        }
        for m in live_materials
        if m.is_below_critical
    ]
    # «На исходе» и «нет в наличии» считаем врозь — как каталог: ноль там
    # спокойный факт (только что заведённый каталог весь на нуле), красное
    # — когда остаток есть, но упал до порога. Плитка «Материалов на
    # исходе: 16» на свежей базе с одним материалом на исходе выглядела
    # аварией и спорила со складом, где на исходе был один.
    out_of_stock_count = sum(1 for m in low_stock_items if m["quantity"] <= 0)
    low_stock_count = len(low_stock_items) - out_of_stock_count

    return {
        "unrealised_asset": stock_value,
        "revenue": {
            "cash": revenue_cash,
            "mbank": revenue_mbank,
            "demirbank": revenue_demirbank,
            "online": revenue_online,
            "total": revenue_total,
            # Выручка — это ЗАКАЗЫ периода, включая отданные в долг.
            # Сколько из них уже на руках и сколько ещё должны — тем же
            # разрезом, чтобы плитку нельзя было прочитать как кассу.
            "received": split(received),
            "debt": split(debt),
        },
        "breakdown": {
            "work_revenue": work_revenue,
            # ПРИБЫЛЬ ДО РАСХОДОВ — выручка минус себестоимость всего
            # проданного (решение владельца, 2026-08-27). Одной выручки
            # наверху было мало: «5 525» читалось как заработок, хотя
            # 2 955 из них — деньги, за которые материал купили. Это
            # НЕ выручка (выручка честно равна 5 525) и НЕ итоговая
            # прибыль (из неё ещё вычитаются аренда и зарплаты).
            #
            # Себестоимость берём по ВСЕМ строкам, а не только по
            # материалу: услуга списывает по техкарте свой расходник
            # (клей, крепёж), и его стоимость сидит в строке РАБОТЫ.
            # Считать её только по материалу значило бы разойтись с
            # «Финансами» — а это одно и то же число на двух экранах.
            #
            # С 2026-10-07 это ВАЛОВАЯ ПРИБЫЛЬ ОПиУ — после потерь материала
            # (D-15), одним числом с «Финансами» (D-16).
            "profit_before_expenses": p["gross_profit"],
            "losses": p["losses"],
            # Себестоимость всего проданного — чтобы подпись под плиткой
            # объясняла её же формулой, а не складывала работу с
            # материалом: расходники услуги в такую сумму не попадают,
            # и подпись расходилась с плиткой на их стоимость.
            "cogs_total": p["cogs_material"] + p["cogs_services"],
            # Материал: за сколько продали, почём он нам достался и что
            # осталось. Прибыль тут ВАЛОВАЯ — до аренды, зарплат и
            # прочих расходов; итоговую прибыль по-прежнему считают
            # Финансы блоком «Материалы» (решение заказчика).
            "material_revenue": material_revenue,
            "material_cost": material_cost,
            "material_profit": material_revenue - material_cost,
        },
        "services_performed": services_count,
        "materials_consumed_by_services": materials_consumed,
        "refunds": {
            "total_refunded": refunded_total,
            # Сколько денег вынесли со склада мимо продажи, сколько
            # таких записей и у скольких из них себестоимость
            # неизвестна (списания до 04.09).
            "material_lost_cost": lost_cost,
            "material_lost_rows": lost_rows,
            "material_lost_unknown": lost_unknown,
        },
        "low_stock_count": low_stock_count,
        "out_of_stock_count": out_of_stock_count,
        "low_stock_items": low_stock_items,
    }
