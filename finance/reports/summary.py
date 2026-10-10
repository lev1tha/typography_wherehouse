"""«Сводка» «Финансов» — отчёт как Excel заказчика.

Блоки трат («Материалы», «Постоянные», «Переменные», «Инвестиции», «Проценты и
налоги») с подытогами, выручка, долг клиентов, склад (оборот) с цепочкой на
конец периода, резка по станкам и сотрудникам, обрезки, долг поставщикам.

Денежные итоги — выручка, себестоимость, валовая прибыль, расходы, прибыль —
берутся из ОПиУ (`pnl.pnl`): «Сводка» за месяц и ОПиУ за этот месяц — одно и то же
число, а не два расчёта, которые сверяет тест.

Перенесено из `FinanceReportView` (2026-10-07, этап 2): обработчик теперь только
читает период и отдаёт результат.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q
from django.utils import timezone

from accounts.models import Employee, User
from sales import reporting
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import Material, Roll, Supplier, Supply, stock_value_total
from warehouse.supplier_ledger import supplier_balance

from ..material_sheet import purchases_from_stock, q2
from ..models import ExpenseEntry, ExpenseKind
from .money import SUM as _SUM
from .pnl import pnl
from .work import by_service
from .work import machine_table as work_table


def _supply_line_label(line) -> str:
    """Как строка накладной была принята: «Лист 1.22×2.44 ×10», «12 м», «50 шт»."""
    if line.roll_id:
        return line.roll.dimensions_label
    n = lambda v: format(Decimal(v).normalize(), "f")  # noqa: E731
    if line.form == "SHEET" and line.width and line.height and line.sheet_count:
        return f"Лист {n(line.width)}×{n(line.height)} ×{n(line.sheet_count)}"
    if line.form == "ROLL" and line.length:
        return f"{n(line.length)} м"
    return f"{n(line.quantity)} {line.material.get_unit_display()}"


def supplier_debts():
    """Кому и сколько должен цех за материал — накладные и партии «в долг».

    С ДОКУМЕНТОМ на каждой строке: номер, поставщик, дата, сумма, сколько
    заплачено и что именно пришло. Одной цифрой «должны 22 550» долг не
    доказать — владелец спрашивал «за что?», а накладной видно не было."""
    rows = []
    supplies = Supply.objects.select_related("supplier", "created_by").prefetch_related(
        "lines__material", "lines__roll", "payments", "returns"
    )
    for supply in supplies:
        debt = supply.debt
        if debt > 0:
            rows.append({
                "kind": "SUPPLY",
                "id": supply.id,
                "label": f"Накладная {supply.number or f'#{supply.id}'}",
                "supplier": supply.supplier.name if supply.supplier_id else "",
                "date": supply.received_on,
                "total": supply.total_cost,
                # Старое поле накладной + платежи-строки (2026-10-10).
                "paid": supply.paid_total,
                "debt": debt,
                "currency": supply.currency,
                "debt_foreign": supply.debt_foreign,
                "note": supply.note,
                "created_by": supply.created_by.username if supply.created_by_id else "",
                "lines": [
                    {"material": line.material.name, "what": _supply_line_label(line), "cost": line.cost}
                    for line in supply.lines.all()
                ],
            })
    for lot in Roll.objects.filter(supplier_debt__gt=0).select_related("material", "created_by"):
        rows.append({
            "kind": "LOT",
            "id": lot.id,
            "label": f"Приход «{lot.material.name}»",
            "supplier": lot.code,
            "date": timezone.localtime(lot.received_at).date(),
            "total": lot.purchase_cost,
            "paid": lot.purchase_cost - lot.supplier_debt,
            "debt": lot.supplier_debt,
            "note": "",
            "created_by": lot.created_by.username if lot.created_by_id else "",
            "lines": [
                {"material": lot.material.name, "what": lot.dimensions_label, "cost": lot.purchase_cost}
            ],
        })
    # Начальные долги, авансы и переплаты поставщикам — той же формулой, что
    # карточка поставщика: сальдо = начальный долг + накладные − платежи.
    # Строки накладных выше — их долги по документам; здесь разница между
    # сальдо поставщика и суммой этих долгов (начальный долг — плюс, аванс и
    # переплата, закрывающие долги других накладных, — минус).
    for supplier in Supplier.objects.prefetch_related(
        "supplies__lines", "supplies__payments", "supplies__returns", "payments__offsets",
        "opening_debts",
    ):
        balance = supplier_balance(supplier)
        adjust = balance["owe"] - sum((s.debt for s in supplier.supplies.all()), Decimal("0"))
        if adjust:
            rows.append({
                "kind": "LEDGER",
                "id": supplier.id,
                "label": "Начальный долг и взаимозачёт аванса" if adjust > 0 else "Аванс и переплата поставщику",
                "supplier": supplier.name,
                "date": min(
                    [d.as_of for d in supplier.opening_debts.all()]
                    + [p.paid_on for p in supplier.payments.all()] + [timezone.localdate()]
                ),
                "total": balance["opening"], "paid": balance["paid"], "debt": adjust,
                "note": "", "created_by": "", "lines": [],
            })
    rows.sort(key=lambda r: (r["date"], r["kind"], r["id"]))
    return {"total": sum((r["debt"] for r in rows), Decimal("0")), "rows": rows}




def finance_summary(d_from=None, d_to=None) -> dict:
    """«Сводка» за период (границы включительные, None — без границы)."""
    # ОПиУ периода — источник ВСЕХ денежных итогов «Сводки»: выручка,
    # себестоимость, валовая прибыль, расходы, прибыль. Раньше «Сводка»
    # считала их своими формулами, а ОПиУ — своими, и совпадали они только
    # потому, что их сверял тест.
    p = pnl(d_from, d_to)

    def by_sold(qs, field="revenue_recognized_at"):
        # Продажи — по дню признания выручки: у обычного заказа это день
        # заказа, у онлайн-заказа — день подтверждения оплаты; неоплаченный
        # онлайн-счёт сюда не попадает вовсе (D-7, D-14).
        qs = qs.filter(**{f"{field}__isnull": False})
        if d_from:
            qs = qs.filter(**{f"{field}__date__gte": d_from})
        if d_to:
            qs = qs.filter(**{f"{field}__date__lte": d_to})
        return qs

    def by_spent(qs):
        if d_from:
            qs = qs.filter(spent_at__gte=d_from)
        if d_to:
            qs = qs.filter(spent_at__lte=d_to)
        return qs

    # Структура отчёта повторяет Excel заказчика: блоки с подытогами —
    # Материалы, Постоянные, Переменные, Инвестиции, Проценты и налоги. Строки
    # блоков — справочник ExpenseKind: админ заводит свои виды («Реклама»,
    # «Ремонт станка»), и они сами появляются в нужном блоке.
    #
    # Сумма строки зависит от роли вида (D-2):
    #   расход и проценты — НАЧИСЛЕНО за период («за какой месяц»), из ОПиУ;
    #   покупка в «Инвестициях», уплата налога, закуп, долг материала —
    #   ОПЛАЧЕНО за период (по дате траты): у них в ОПиУ своя строка или нет её.
    spent_by_kind = defaultdict(lambda: Decimal("0"))
    # Записи без денег (карточка актива в рассрочку, начисление зарплаты) в
    # «оплачено» не входят: деньги по ним идут платежами и выплатами.
    for row in (
        by_spent(ExpenseEntry.objects.filter(is_cashless=False))
        .values("kind_id")
        .annotate(v=_SUM("amount"))
    ):
        spent_by_kind[row["kind_id"]] = row["v"]
    accrued = {
        r["kind_id"]: r["amount"] for block in p["opex"]["blocks"] for r in block["rows"]
    }

    # Скрытый вид остаётся в отчёте, пока в периоде по нему есть траты.
    # «Удалить» вид с историей его прячет, а не удаляет — и если бы отчёт
    # брал только видимые, аренда прошлых месяцев исчезала бы из прибыли
    # одним нажатием (+25 000 к сентябрю), хотя деньги давно ушли, а
    # график по дням продолжал бы их считать.
    kinds = [
        k for k in ExpenseKind.objects.all()
        if not k.is_archived or spent_by_kind.get(k.id) or accrued.get(k.id)
    ]

    # Что система считает сама, чтобы это не пришлось вбивать руками.
    # Закуп материала складывается из приходов на склад: каждое поступление
    # уже несёт свою себестоимость. Старые ручные записи по этому же виду к
    # нему ДОБАВЛЯЮТСЯ — справочно.
    stock_purchases = purchases_from_stock(d_from, d_to)
    auto_by_code = {ExpenseKind.MATERIAL_PURCHASE: stock_purchases}
    Role = ExpenseKind.Role

    def block_rows(block):
        """Строки блока со суммой за период, в порядке отображения."""
        rows = []
        for k in kinds:
            if k.block != block:
                continue
            if k.role == Role.OPEX:
                auto, manual = Decimal("0"), accrued.get(k.id, Decimal("0"))
            elif k.role == Role.INTEREST:
                auto, manual = Decimal("0"), p["interest"]
            else:
                auto = auto_by_code.get(k.code, Decimal("0"))
                manual = spent_by_kind.get(k.id, Decimal("0"))
            rows.append({
                "id": k.id,
                "code": k.code,
                "name": k.name,
                # Входит ли строка в «Расходы» этого блока (операционный
                # расход). Покупки, налог и закуп — справочно: у них своя
                # строка ОПиУ (амортизация, налог) или нет её вовсе.
                "in_profit": k.role == Role.OPEX,
                "role": k.role,
                # Трата этого вида двигает кассу? (BAD_DEBT — нет.)
                "moves_cash": k.moves_cash,
                # По какой дате сумма: начислено («за какой месяц») или
                # оплачено (дата траты).
                "basis": "accrued" if k.role in ExpenseKind.PROFIT_ROLES else "paid",
                "is_builtin": k.is_builtin,
                "amount": auto + manual,
                # Часть, посчитанная системой: интерфейс помечает её и
                # объясняет, откуда взялась цифра.
                "auto_amount": auto,
                "manual_amount": manual,
            })
        return rows

    def block_total(rows):
        return sum((r["amount"] for r in rows if r["in_profit"]), Decimal("0"))

    fixed_rows = block_rows(ExpenseKind.Block.FIXED)
    variable_rows = block_rows(ExpenseKind.Block.VARIABLE)
    material_rows = block_rows(ExpenseKind.Block.MATERIALS)
    total_fixed = block_total(fixed_rows)
    operating_variable = block_total(variable_rows)

    # Инвестиции — покупки оборудования и улучшение цеха по дате оплаты. От
    # порога капвложения — актив: в прибыль идёт амортизацией (строка ОПиУ),
    # дешевле — сразу расход месяца и входит в «Расходы» (D-13, D-22).
    investment_rows = block_rows(ExpenseKind.Block.INVESTMENT)
    capex = by_spent(ExpenseEntry.objects.filter(kind__role=Role.CAPEX))
    capitalized = capex.filter(useful_life_months__isnull=False).aggregate(v=_SUM("amount"))["v"]
    expensed_block = next(
        (b for b in p["opex"]["blocks"] if b["block"] == ExpenseKind.Block.INVESTMENT), None
    )
    investments = {
        "rows": investment_rows,
        "total": sum((r["amount"] for r in investment_rows), Decimal("0")),
        "capitalized": capitalized,
        # Покупки дешевле порога, начисленные в периоде, — часть «Расходов».
        "expensed": expensed_block["total"] if expensed_block else Decimal("0"),
        "depreciation": p["depreciation"] + p["disposal"],
    }
    # Проценты по займам (начислено, в прибыли) и уплата налога (деньги).
    # Сам налог начисляется от выручки — строкой `tax`.
    below_rows = block_rows(ExpenseKind.Block.BELOW)
    below = {
        "rows": below_rows,
        "interest": p["interest"],
        "tax": p["tax"],
        "tax_label": p["tax_label"],
    }

    # Себестоимость проданного: закупочная стоимость материала и расходников,
    # ушедших в продажи периода (FIFO, снимок в момент продажи), минус
    # возвращённое в периоде. Потери материала — отдельно (`losses`).
    # Гарантийные переделки (волна 2) — часть себестоимости своей строкой ОПиУ;
    # в цепочке склада они тоже ушли с полок, поэтому складываются обратно.
    cogs = p["cogs_material"] + p["cogs_services"] + p["cogs_warranty"]

    # --- Блок «Материалы» ------------------------------------------------
    # Себестоимость проданного в итог блока НЕ входит (решение владельца,
    # 2026-08-27): она в себестоимости ОПиУ, до валовой прибыли. Расход
    # блока — транспорт и свои строки.
    materials_spend = block_total(material_rows)
    materials = {
        "spend": materials_spend,            # операционные строки блока
        "cogs": cogs,                        # СПРАВОЧНО: в итог блока не входит
        # Из неё — себестоимость гарантийных переделок (своя строка ОПиУ).
        "cogs_warranty": p["cogs_warranty"],
        "rows": material_rows,
        "total": materials_spend,
    }

    # --- Склад (оборот) --------------------------------------------------
    # Закуп за период — сколько материала ПРИШЛО на склад (по приходам, у
    # каждого своя цена); стоимость склада — сколько лежит на конец периода.
    # Потери (брак, недостача) — из ОПиУ: с 2026-10-07 они в себестоимости,
    # до валовой прибыли (D-15).
    loss_cost, loss_unknown = p["losses"], p["losses_unknown"]

    # СХОДИМОСТЬ СКЛАДА — «куда делись деньги» ЗА ВЫБРАННЫЙ ПЕРИОД:
    #
    #   было на начало + закуп − себестоимость проданного − списано
    #     = должно лежать на конец
    #
    # Раньше блок считался за всю историю, потому что остаток система знала
    # только «на сейчас». Теперь знает на любой день, и цепочка идёт по
    # тому же периоду, что и все остальные плитки. Без неё «Склад (оборот)»
    # показывал остаток, а читался как оборот: заказчик ждал 1 544 280
    # (начало плюс приходы) и не находил в нём вычета проданного.
    #
    # «Весь период» даёт ту же картину за всю историю — выбор месяца её
    # только сужает.
    opening = (
        stock_value_total(d_from - timedelta(days=1)).quantize(Decimal("0.01"))
        if d_from else Decimal("0.00")
    )
    all_purchases = purchases_from_stock(d_from, d_to)
    all_cogs = cogs
    all_loss, all_loss_unknown = loss_cost, loss_unknown

    # ОСТАТОК СВЕРХ ПАРТИЙ — часть стоимости склада, за которой нет
    # прихода: инвентаризация правит количество, партий не создавая, и
    # такой «хвост» оценивается последней закупочной ценой (так же его
    # считает `Material.stock_value`). На проде 19.09 это 3 980 сом:
    # «диот жёлтый» куплен в количестве 4 штук на 16 сом, а пересчёт
    # поставил 800 — и склад подорожал на 3 184 из воздуха. Цифра
    # объясняет часть разрыва, поэтому стоит рядом с ним.
    stock_without_lots = Decimal("0")
    for m in Material.objects.filter(quantity__gt=0).prefetch_related("rolls"):
        in_lots = sum((r.remaining_area for r in m.rolls.all()), Decimal("0"))
        tail = (m.quantity or Decimal("0")) - in_lots
        if tail > 0:
            stock_without_lots += tail * (m.purchase_price or Decimal("0"))
    # Стоимость склада — той же функцией, что «Стоимость склада» в «Обзоре».
    # Своя формула здесь (все партии + количество × закупочную у штучных)
    # считала штучные партии дважды: приход партии поднимает и её остаток,
    # и `quantity` материала. На проде 15.09 — 1 386 543 против 1 180 737.
    #
    # Склад — на конец ВЫБРАННОГО периода: и плиткой, и концом цепочки.
    # Раньше плитка показывала сегодняшний склад в любом месяце: откроешь
    # август, где ни продаж, ни приходов, — везде нули, а склад 1 184 614.
    # Цифра из другого времени стояла среди месячных.
    value_period = stock_value_total(d_to).quantize(Decimal("0.01"))
    expected = opening + all_purchases - all_cogs - all_loss
    stock = {
        "purchases": stock_purchases,
        "value_now": value_period,
        # На какой день посчитан склад: null — на сегодня. Интерфейс по
        # этому полю и подписывает плитку, чтобы дата была видна.
        "as_of": d_to.isoformat() if (d_to and d_to < timezone.localdate()) else None,
        # Потери периода — брак и недостача по себестоимости. Входят в
        # прибыль отдельной строкой (`losses` верхнего уровня).
        "losses": loss_cost,
        "losses_unknown": loss_unknown,
        "reconcile": {
            # Сколько лежало на складе в день перед началом периода.
            "opening": opening,
            "purchases": all_purchases.quantize(Decimal("0.01")),
            "cogs": all_cogs,
            "losses": all_loss,
            "losses_unknown": all_loss_unknown,
            "expected": expected.quantize(Decimal("0.01")),
            # Конец цепочки — остаток на конец ПЕРИОДА, а не «на сегодня»:
            # иначе строки складывались бы за месяц, а итог был бы за год.
            "value_now": value_period,
            # Что не объясняется ни продажей, ни списанием: остаток,
            # заведённый инвентаризацией без прихода («было на полке до
            # системы»), приход без цены, старые списания без
            # себестоимости. Ноль тут — редкость; важно, чтобы цифра
            # стояла на экране, а не пряталась в разнице двух других.
            "gap": (expected - value_period).quantize(Decimal("0.01")),
            # Из чего разрыв складывается чаще всего: остаток, у которого
            # нет прихода (его завела инвентаризация), — он тянет разрыв в
            # минус, и старые списания без себестоимости — в плюс.
            "stock_without_lots": stock_without_lots.quantize(Decimal("0.01")),
        },
    }

    # РАСХОДЫ — операционные расходы ОПиУ: транспорт и свои строки блока
    # «Материалы», постоянные, переменные, покупки дешевле порога и старые
    # расходы, внесённые прямо в кассу. Себестоимости здесь НЕТ: она
    # вычитается выше, до валовой прибыли.
    total_expenses = p["opex"]["total"] + p["opex_cash_manual"]

    # Выручка = ВСЕ продажи периода (по дню признания) минус возвраты,
    # оформленные в этом периоде (по дате возврата). Заказ, отданный в
    # долг, — тоже выручка: работа сделана, материал списан. Сколько из неё
    # уже на руках (`revenue_paid`) и сколько ещё должны (`client_debt`) —
    # отдельными строками, по заказам периода и на сегодня.
    #
    # Возврат по заказу прошлого месяца уменьшает выручку ЭТОГО: раньше он
    # вычитался из месяца заказа, и принятый отчёт менялся задним числом.
    live = by_sold(Receipt.objects.exclude(status=Receipt.Status.CANCELLED))
    revenue = p["revenue"]
    refunds = reporting.refunds(d_from, d_to)
    # Сколько денег по этим заказам реально приняли — включая предоплаты.
    #
    # По каждому чеку берём НЕ БОЛЬШЕ его вклада в выручку: возврат уменьшает
    # выручку, а `amount_paid` остаётся прежним, и «оплачено» вылезало выше
    # «выручки» — 13 613 против 13 253 при нулевом долге. Плитка сама себе
    # противоречила, и это первое, что заказчик складывает в уме.
    # Лишнее сверх стоимости оставшихся строк — это уже не выручка, а сдача
    # или возвращённые деньги, и они живут в своих полях.
    revenue_paid = Decimal("0")
    for r in live.only("total_price", "amount_paid", "refunded_amount"):
        kept = r.total_price - r.refunded_amount
        revenue_paid += min(r.amount_paid, kept) if kept > 0 else Decimal("0")
    pending = live.filter(payment_status__in=Receipt.OWING_STATUSES)

    # Долг клиентов = Σ (сумма − предоплата − возвраты) по открытым чекам.
    #
    # Заказы БЕЗ КЛИЕНТА считаем отдельной строкой. Они входят в общий долг
    # (деньги цеху правда не принесли), но спросить их не с кого: в
    # карточках клиентов их нет, и сумма долгов по «Клиентам» не сходилась
    # с этой плиткой — на проде 19.09 это 304 038 против 314 141, и
    # 10 103 разницы объяснить было нечем. Четыре заказа, оформленные
    # целиком в долг и без имени.
    client_debt = Decimal("0")
    anonymous_debt = Decimal("0")
    for r in pending.only("total_price", "amount_paid", "refunded_amount", "client_id"):
        owed = r.total_price - r.amount_paid - r.refunded_amount
        if owed > 0:
            client_debt += owed
            if not r.client_id:
                anonymous_debt += owed
    # Входящий долг на дату переезда из Excel (волна 2): не выручка, но долг —
    # в плитке он стоит в периоде, куда попала дата переезда (как заказ — днём
    # заказа). Отдельной цифрой тоже: откуда долг без продаж.
    from clients.opening import open_debts_qs

    opening = open_debts_qs()
    if d_from:
        opening = opening.filter(as_of__gte=d_from)
    if d_to:
        opening = opening.filter(as_of__lte=d_to)
    opening_debt = opening.aggregate(v=_SUM("remaining"))["v"] or Decimal("0")
    client_debt += opening_debt

    # Резка — по СТАНКАМ: ЧПУ и лазер. Раньше строки были по типу материала
    # (Акрил / Форекс / Оргстекло), но заказчик считает работу цеха станками:
    # «сколько наработал ЧПУ, сколько лазер» — это его вопрос, а материал в
    # нём вторичен. Материал никуда не делся: он остался в складском листе,
    # где у каждого материала стоит своя выручка с резки.
    cut_by_machine = defaultdict(lambda: Decimal("0"))
    # Объём работы двумя величинами — одной суммы мало: 12 000 сом это много
    # мелких резов или один большой лист?
    #   ПОГОННЫЕ метры — длина реза, по ней и считается сумма (пог.м × ставка);
    #   КВАДРАТНЫЕ — площадь резаного куска, берётся у материала того же чека.
    # Это РАЗНЫЕ величины, складывать их между собой нельзя.
    area_by_machine = defaultdict(lambda: Decimal("0"))
    pm_by_machine = defaultdict(lambda: Decimal("0"))
    # Сколько квадратных метров прошло через руки каждого сотрудника. Это
    # ответ на «кто сколько отрезал» — вопрос про ЛЮДЕЙ, а не про деньги,
    # поэтому и считается в метрах.
    area_by_user = defaultdict(lambda: Decimal("0"))
    pm_by_user = defaultdict(lambda: Decimal("0"))
    rev_by_user = defaultdict(lambda: Decimal("0"))
    cutting_total = Decimal("0")
    # ОБРЕЗКИ: сколько материала списали, но клиенту не отдали. Полосу 0.5 м
    # от рулона 0.9 отрезают на всю ширину, и 0.4 остаётся в цехе — обычно в
    # мусор. Деньги за полную ширину взяты, и это правильно: материал
    # потрачен весь. Но цифра «сколько я подарил» не считалась НИГДЕ.
    #
    # Это не добавочный расход — он уже сидит в себестоимости проданного.
    # Здесь только та его часть, которая до клиента не дошла.
    offcut_area = Decimal("0")
    offcut_cost = Decimal("0")
    for item in (
        by_sold(
            # Рулон по кв.м изделия (CALC-10) — ширина изделия в `width`.
            TransactionItem.objects.filter(Q(used_width__isnull=False) | Q(roll_area=True)),
            field="receipt__revenue_recognized_at",
        )
        .select_related("material", "roll", "receipt")
    ):
        # Обрезок строки, возвращённой ПОЗЖЕ периода, в периоде был.
        # `offcut_area` у возвращённой строки даёт ноль — считаем по живой.
        if reporting.counts_at(item, d_to):
            was = item.is_returned
            item.is_returned = False
            offcut_area += item.offcut_area
            offcut_cost += item.offcut_cost
            item.is_returned = was
    cutting_area = Decimal("0")
    cutting_pm = Decimal("0")

    def is_cut(i):
        return (
            i.type == TransactionItem.Type.SERVICE
            and i.service_id
            and i.service.kind == "CUTTING"
        )

    # Заказы периода — в любом статусе: возвращённый ПОЗЖЕ заказ в своём
    # месяце был работой. Строки берём живые на конец периода; возврат,
    # оформленный в периоде, их уже убрал.
    cut_receipts = (
        by_sold(
            Receipt.objects.filter(
                items__type=TransactionItem.Type.SERVICE,
                items__service__kind="CUTTING",
            )
        )
        .distinct()
        .select_related("cashier")
        .prefetch_related("items__material__type", "items__service", "items__roll", "items__executor")
    )

    def worker(line, receipt):
        """Кому строка резки в «Резке по сотрудникам»: исполнитель строки
        (волна 2), а у строк без него — как раньше, кто оформил заказ."""
        if line.executor_id:
            return f"e{line.executor_id}"
        return receipt.cashier_id

    for r in cut_receipts:
        items = list(r.items.all())
        # Строки работы мастера. Их количество — это и есть длина реза в
        # погонных метрах, из неё же складывается сумма.
        cut_lines = [i for i in items if is_cut(i) and reporting.counts_at(i, d_to)]
        if not cut_lines:
            continue
        # Площадь резаного материала этого чека. Продажа по кв.м даёт её
        # прямо в количестве (только у материала с единицей «кв.м»); продажа
        # листами — через площадь листа; рулон — длина × ширина полотна.
        # Штучный материал (крепёж, саморезы) площади не имеет и в кв.м не
        # попадает — ни режимом «целиком», ни режимом «по площади».
        #
        # ВОЗВРАЩЁННЫЕ строки материала СЧИТАЮТСЯ ТОЖЕ. Это метрика работы
        # станка — «сколько прошло через ЧПУ», — а станок отрезал независимо
        # от того, вернул клиент материал потом или нет. Раньше возврат
        # обнулял площадь, а строка работы оставалась живой, и плитка
        # показывала «Лазер 0 кв.м · 444 сом»: денег насчитали, а работы
        # будто не было. Деньги здесь берутся со строки РАБОТЫ и на возврат
        # материала не реагируют — значит и площадь не должна.
        area = Decimal("0")
        for i in items:
            if i.type != TransactionItem.Type.MATERIAL or not i.material_id:
                continue
            if i.sale_mode == TransactionItem.SaleMode.PIECE:
                if i.material.piece_area:
                    area += i.quantity * i.material.piece_area
            elif i.sale_mode == TransactionItem.SaleMode.METER:
                # Ширина ПАРТИИ, с которой резали (у карточки — лишь
                # значение по умолчанию для приёмки).
                if i.roll_width:
                    area += i.quantity * i.roll_width
            elif i.roll_area:
                # Рулон по кв.м изделия (CALC-10): через станок прошла длина
                # изделия на всю ширину рулона — как у продажи метрами.
                if i.roll_width and i.length:
                    area += i.length * i.roll_width
            elif i.material.unit in (Material.Unit.SQM, Material.Unit.METER):
                # «По площади»: количество строки — это и есть кв.м, но только
                # у листового/рулонного материала (кв.м, пог.м). Штучный
                # материал (саморезы ×100) без режима получал «по площади» по
                # умолчанию, и 100 штук уходили в «Резка, всего» как 100 кв.м.
                area += i.quantity

        receipt_pm = sum((i.quantity for i in cut_lines), Decimal("0"))
        workers = {worker(line, r) for line in cut_lines}
        for idx, line in enumerate(cut_lines):
            machine = line.service.machine or ""
            # ТА ЖЕ сумма, что стоит в чеке: строка округляется вверх до
            # целого сома. Через `quantity × price` отчёт расходился с
            # чеками на копейки, и сверка «по бумаге» переставала сходиться
            # ровно там, где заказчик её и делает.
            rev = line.sold_total
            cut_by_machine[machine] += rev
            pm_by_machine[machine] += line.quantity
            cutting_total += rev
            cutting_pm += line.quantity
            # Площадь у чека одна на всех, а станков в нём может быть два.
            # Делим её пропорционально длине реза: у чека с одним станком
            # (обычный случай) он получает всю площадь целиком.
            #
            # Погонные метры вводятся ВРУЧНУЮ и могут быть не введены — тогда
            # делить не по чему. Раньше в этом случае доля выходила нулевой,
            # и площадь попадала в «Резка, всего», но ни в одну строку
            # станка: в итоге «всего 1,56 кв.м», а под ним «ЧПУ 0 кв.м».
            # Делим поровну — резали же на этих станках, сколько бы метров
            # ни забыли вписать.
            if receipt_pm:
                share = line.quantity / receipt_pm
            else:
                share = Decimal("1") / Decimal(len(cut_lines))
            area_by_machine[machine] += area * share
            # Сотруднику площадь отдаём целиком и один раз: чек оформил один
            # человек, и дробить её между строками того же чека незачем. Если у
            # строк чека РАЗНЫЕ исполнители — площадь делится так же, как между
            # станками (по длине реза).
            who = worker(line, r)
            if len(workers) == 1:
                if idx == 0:
                    area_by_user[who] += area
            else:
                area_by_user[who] += area * share
            pm_by_user[who] += line.quantity
            rev_by_user[who] += rev
        cutting_area += area

    # Возвраты работы, оформленные в периоде, по заказам ПРОШЛЫХ периодов:
    # деньги уходят из этого периода, со станка и сотрудника того заказа.
    # Метры и площадь не трогаем — резали тогда, и работа была.
    for line in reporting.prior_returns(d_from, d_to).select_related("service", "receipt"):
        if not is_cut(line):
            continue
        machine = line.service.machine or ""
        cut_by_machine[machine] -= line.sold_total
        cutting_total -= line.sold_total
        rev_by_user[worker(line, line.receipt)] -= line.sold_total

    # Строки — станки, по которым в периоде что-то резали. «Без станка» —
    # старые чеки, оформленные до разделения, если у их услуги станок не
    # проставлен: молча прятать их сумму нельзя, иначе строки не сойдутся
    # с «Резка, всего».
    machine_names = dict(PrintingService.Machine.choices)
    # «% ЗП мастера» из настроек цен — доля от стоимости работы резки.
    # Настройка существовала, но нигде не считалась: владелец ставил 4 % и
    # ждал цифру, которой не было. Показываем РАСЧЁТНУЮ долю — справочно,
    # в прибыль она не входит: зарплаты вносятся записями, иначе счёт
    # двойной. Целыми сомами, как и остальные деньги отчёта.
    from services.models import PricingSettings

    master_pct = PricingSettings.load().master_commission_percent or Decimal("0")

    def master_share(amount):
        # Целый сом, половина вверх (STAFF-14): 58,5 — это 59, а не 58, как давало
        # банковское округление по умолчанию.
        return (amount * master_pct / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    # Сортируем строки по ПЛОЩАДИ, а не по сумме: главная величина блока —
    # квадратные метры, и порядок строк должен объяснять именно её.
    # Работы по станкам целиком (STAFF-03/-04): гравировка и монтаж — колонкой
    # «прочие работы», возврат — колонкой, ряд по дням. Станок, у которого вся
    # резка периода возвращена, остаётся строкой: возврат не стирает станок.
    table = work_table(d_from, d_to)
    machines = (
        set(cut_by_machine) | set(area_by_machine) | set(table["cutting"]) | set(table["other"])
    )
    user_names = dict(
        User.objects.filter(
            id__in=[u for u in area_by_user if u and not isinstance(u, str)]
        ).values_list("id", "username")
    )
    # Исполнители строк (волна 2) — ключ «e<id сотрудника>», имя — ФИО.
    user_names.update({
        f"e{eid}": name
        for eid, name in Employee.objects.filter(
            id__in=[int(u[1:]) for u in area_by_user if isinstance(u, str)]
        ).values_list("id", "full_name")
    })
    cutting = {
        "total": cutting_total,
        "area": q2(cutting_area),
        "running_meters": q2(cutting_pm),
        # Расчётная ЗП мастера от всей работы резки за период — справочно.
        "master_commission_percent": master_pct,
        "master_share": master_share(cutting_total),
        "rows": [
            {
                "id": machine or None,
                "name": machine_names.get(machine) or "Без станка",
                "amount": cut_by_machine.get(machine, Decimal("0")),
                "area": q2(area_by_machine.get(machine, Decimal("0"))),
                "running_meters": q2(pm_by_machine.get(machine, Decimal("0"))),
                # Резка, проданная в периоде (в том числе возвращённая позже или
                # в нём же), и возвраты резки периода: amount = sold − returned.
                "sold": table["cutting"].get(machine, {}).get("sold", Decimal("0")),
                "returned": table["cutting"].get(machine, {}).get("returned", Decimal("0")),
                # Прочие работы станка (гравировка, монтаж…), нетто возвратов.
                "other_amount": (
                    table["other"].get(machine, {}).get("sold", Decimal("0"))
                    - table["other"].get(machine, {}).get("returned", Decimal("0"))
                ),
                "other_returned": table["other"].get(machine, {}).get("returned", Decimal("0")),
            }
            for machine in sorted(
                machines, key=lambda m: (-area_by_machine.get(m, Decimal("0")), m)
            )
        ],
        # Ряд по дням: что сделано каждым станком, прочие работы, возвраты, итог.
        "days": table["days"],
        "other_total": sum(
            (v["sold"] - v["returned"] for v in table["other"].values()), Decimal("0")
        ),
        # Кто сколько отрезал. С волны 2 — по ИСПОЛНИТЕЛЮ строки резки
        # (`TransactionItem.executor`, ключ «e<id>», имя — ФИО сотрудника), а
        # у строк без исполнителя — как раньше, по тому, КТО ОФОРМИЛ ЗАКАЗ
        # (ключ — id учётки, имя — логин).
        "by_user": [
            {
                "id": uid,
                "kind": "employee" if isinstance(uid, str) else "user",
                "name": user_names.get(uid) or "Без сотрудника",
                "area": q2(area),
                "running_meters": q2(pm_by_user.get(uid, Decimal("0"))),
                # Стоимость работы реза этого сотрудника и его расчётная доля.
                "amount": rev_by_user.get(uid, Decimal("0")),
                "master_share": master_share(rev_by_user.get(uid, Decimal("0"))),
            }
            for uid, area in sorted(area_by_user.items(), key=lambda kv: -kv[1])
        ],
    }

    from sales.pricing_rules import rules_summary

    return {
        # Правила прайса за период (2026-10-10): сколько взяли минимумом и
        # срочностью и сколько отдали скидками. Уже внутри выручки — это
        # раскладка «откуда разница с каталогом», а не отдельные деньги.
        "pricing_rules": rules_summary(d_from, d_to),
        # Строки блоков — виды расхода из справочника, в порядке
        # отображения. Пояснения «что входит» живут примечанием у
        # каждой записи траты.
        "fixed": {"rows": fixed_rows, "total": total_fixed},
        # Блок «Материалы» — первый в отчёте, как в Excel заказчика.
        "materials": materials,
        # Вложения показываем в этом же блоке, но в total их нет —
        # total идёт в прибыль, вложения нет.
        "variable": {"rows": variable_rows, "total": operating_variable},
        # Деньги в складе: закуп за период и стоимость полок сейчас.
        # Оборот, а не расход — прибыль он уменьшает по мере продажи.
        "stock": stock,
        # Себестоимость проданного материала — отдельной строкой, чтобы
        # было видно маржу: выручка − себестоимость = сколько заработали
        # на материале до накладных расходов.
        "cogs": cogs,
        # Обрезки — часть этой же себестоимости, не дошедшая до клиента.
        "offcuts": {
            "area": offcut_area.quantize(Decimal("0.01")),
            "cost": offcut_cost.quantize(Decimal("0.01")),
        },
        # Валовая прибыль — то, с чего живёт цех: выручка минус то,
        # почём материал достался нам самим. Именно она, а не выручка,
        # стоит наверху расчёта прибыли: «выручка 5 525» при марже
        # 2 570 читается как заработок, которого не было.
        # С 2026-10-07 — после потерь материала (D-15): валовая прибыль ОПиУ.
        "gross_margin": p["gross_profit"],
        "investments": investments,
        "below": below,
        "total_expenses": total_expenses,
        # Брак и недостача периода по себестоимости — вычитаются из
        # прибыли своей строкой. Это не «Расходы»: из кассы эти деньги
        # не уходят, они ушли со склада. `unknown` — записи без
        # себестоимости (до 04.09), в сумму не входят.
        "losses": {"cost": loss_cost, "unknown": loss_unknown},
        "revenue": revenue,
        # Возвраты, оформленные в периоде, — уже вычтены из выручки.
        "refunds": refunds,
        # Из чего складывается выручка: сколько уже на руках и сколько
        # ещё должны. Одной суммы мало — «выручка 300 000» при 200 000
        # долга и «выручка 300 000» деньгами это разные месяцы.
        "revenue_paid": revenue_paid,
        "client_debt": client_debt,
        # Часть долга, которую не с кого спросить: заказы без клиента.
        # Она ВНУТРИ `client_debt`, а не рядом — иначе итог долга
        # пришлось бы складывать глазами.
        "anonymous_debt": anonymous_debt,
        # Часть долга — входящий долг на дату переезда (внутри `client_debt`).
        "opening_debt": opening_debt,
        # ЧИСТАЯ ПРИБЫЛЬ ОПиУ (2026-10-07): валовая − расходы ± касса −
        # амортизация − проценты − налог. Шестая редакция формулы.
        "profit": p["net_profit"],
        # Всё ОПиУ периода — строки, маржи, EBITDA, налог.
        "pnl": p,
        "cutting": cutting,
        # Выручка и маржа по видам услуг (PNL-06): резка, гравировка, установка,
        # буквы, отходы, прочее — и каждая услуга внутри вида отдельно.
        "services": by_service(d_from, d_to, p),
        # Долг поставщикам НА СЕГОДНЯ — зеркало долга клиентов: сколько
        # цех должен за материал, взятый в долг. Раньше приход «в долг»
        # не оставлял следа, а оплату было некуда провести.
        "suppliers": supplier_debts(),
        "period": {
            "from": d_from.isoformat() if d_from else None,
            "to": d_to.isoformat() if d_to else None,
        },
    }
