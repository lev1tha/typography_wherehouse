"""Единый справочник: категория → строка ОПиУ → строка ОДДС.

Одно место, где записано, куда ложится каждая денежная категория системы. До
2026-10-07 это решали три разных куска кода: пара «блок + входит в прибыль» у
вида расхода (ОПиУ), блок вида и словарь статей кассы в `statements.py` (ОДДС).
Связь была неявной, и свой вид расхода со снятой галочкой уходил из кассы, ни
разу не появившись в ОПиУ (аудит, Б-10–Б-11).

Категорий три сорта:

- **роль вида расхода** (`ExpenseKind.Role`) — траты из «Финансов». Капвложение
  делится надвое: выше порога — актив (ОПиУ видит только амортизацию, ОДДС —
  инвестиции), ниже порога — обычный расход (и в ОДДС тогда операционная:
  по МСФО IAS 7 п. 16 в инвестиции идут только траты, создающие актив);
- **статья кассы** (`CashEntry.Article`) — движения, которые пишет касса сама
  (оплаты, сдача, возвраты, откаты) или вносят руками (владелец, займы,
  переводы, пересчёт);
- **системные строки ОПиУ** — то, что считается, а не вносится: выручка,
  себестоимость, потери материала, налог с выручки, амортизация.

Строка ОДДС по трате — своя у каждого вида (`kind:<id>`, подпись — название
вида), поэтому у ролей OPEX/CAPEX здесь только раздел.

Справочник — данные, а не логика: отчёты (`finance/reports/`) берут отсюда
ключи и подписи, а тест держит полноту — у каждой роли и каждой статьи есть
запись, ни одна категория не теряется.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import CashEntry, ExpenseKind

# --- ОДДС: разделы ------------------------------------------------------------

OPERATING = "operating"
INVESTING = "investing"
FINANCING = "financing"
# Не движение денег: переводы между своими счетами и ввод начального остатка.
# В чистый поток не входят, но остаток на конец без них не сойдётся.
OUTSIDE = "outside"

CASH_SECTIONS = {
    OPERATING: "Операционная деятельность",
    INVESTING: "Инвестиционная деятельность",
    FINANCING: "Финансовая деятельность",
    OUTSIDE: "Вне потока (не меняют итог денег)",
}
FLOW_SECTIONS = (OPERATING, INVESTING, FINANCING)

# --- ОПиУ: строки -------------------------------------------------------------
#
# Порядок — порядок отчёта (D-15: потери материала в себестоимости, до валовой
# прибыли). Итоги (валовая, EBITDA, операционная, чистая) не категории — их
# считает отчёт; здесь строки, в которые что-то ложится.

REVENUE = "revenue"
COGS_MATERIAL = "cogs_material"
COGS_SERVICES = "cogs_services"
# Себестоимость гарантийных переделок (волна 2): часть себестоимости, вынесенная
# своей строкой из материала и расходников. Итог себестоимости не меняется.
COGS_WARRANTY = "cogs_warranty"
LOSSES = "losses"
OPEX = "opex"                      # по блокам «Финансов», каждой статьёй
OPEX_CASH_MANUAL = "opex_cash_manual"
CASH_COUNT = "cash_count"
DEPRECIATION = "depreciation"
DISPOSAL = "disposal"
INTEREST = "interest"
TAX = "tax"
TAX_CASH = "tax_cash"      # та же строка налога, основа «по кассе» (только подпись)

PNL_LINES = {
    REVENUE: "Выручка",
    COGS_MATERIAL: "Себестоимость материала",
    COGS_SERVICES: "Расходники услуг (по техкартам)",
    COGS_WARRANTY: "Гарантийные переделки",
    LOSSES: "Потери материала (брак, недостача)",
    OPEX: "Операционные расходы",
    OPEX_CASH_MANUAL: "Расходы, внесённые прямо в кассе (до 27.09.2026)",
    CASH_COUNT: "Недостача / излишек кассы",
    DEPRECIATION: "Амортизация",
    DISPOSAL: "Списание выбывшего оборудования",
    INTEREST: "Проценты по займам",
    TAX: "Налог ({rate} % от выручки)",
    TAX_CASH: "Налог ({rate} % от полученных денег)",
}


@dataclass(frozen=True)
class Mapping:
    """Куда категория ложится в отчёты.

    `pnl` — строка ОПиУ или None (в прибыль не идёт: деньги без расхода или
    доход начисляется по-другому). `cash_section`/`cash_line`/`cash_label` —
    строка ОДДС; `cash_line=None` у трат — строка по самому виду расхода;
    `cash_section=None` — движения денег нет вовсе.
    """

    pnl: str | None
    cash_section: str | None
    cash_line: str | None = None
    cash_label: str | None = None
    note: str = ""


Role = ExpenseKind.Role
A = CashEntry.Article

# Роль вида расхода → строки. CAPEX — актив (выше порога); ниже порога см.
# `for_expense`.
ROLES = {
    Role.OPEX: Mapping(OPEX, OPERATING, note="Расход месяца «за какой месяц»; деньги — днём оплаты."),
    Role.CAPEX: Mapping(
        DEPRECIATION, INVESTING,
        note="Актив: в ОПиУ — амортизация со следующего месяца, в ОДДС — покупка целиком.",
    ),
    Role.INTEREST: Mapping(
        INTEREST, OPERATING, "interest_paid", "Проценты по займам уплаченные",
        note="Ниже операционной прибыли; в ОДДС — операционная (D-4).",
    ),
    Role.TAX: Mapping(
        None, OPERATING, "tax_paid", "Налог уплаченный",
        note="В ОПиУ налог начисляется сам от выручки (D-10); уплата — только деньги.",
    ),
    Role.INVENTORY: Mapping(
        None, OPERATING, "suppliers", "Оплата поставщикам за материал",
        note="Деньги ушли в склад; в прибыль — себестоимостью проданного.",
    ),
    Role.NOT_CASH: Mapping(None, None, note="Справочная запись: ни денег, ни расхода."),
}

# Платёж по активу, купленному в рассрочку: деньги — инвестиции, в ОПиУ его нет
# (актив уже в прибыли амортизацией карточки на полную цену).
ASSET_PAYMENT = Mapping(
    None, INVESTING,
    note="Платёж по активу в рассрочку: ОДДС — инвестиции, ОПиУ — только амортизация карточки.",
)

# Капвложение ниже порога — обычный расход месяца.
CAPEX_EXPENSED = Mapping(
    OPEX, OPERATING,
    note="Покупка дешевле порога капвложения: расход сразу, в ОДДС — операционная (IAS 7, п. 16).",
)

# Статья кассы → строки. Для записей, привязанных к трате, статья не важна —
# решает роль вида (`for_cash_entry`).
ARTICLES = {
    # Оплата и сдача — одни и те же деньги клиента: принёс, получил сдачу.
    A.SALE: Mapping(None, OPERATING, "clients", "Поступления от клиентов",
                    note="Выручка в ОПиУ — по дате заказа, не по оплате."),
    A.CHANGE: Mapping(None, OPERATING, "clients", "Поступления от клиентов"),
    # D-18: откаты — отдельной строкой, мимо ОПиУ.
    A.UNPAY: Mapping(None, OPERATING, "unpay", "Откаты оплат и удалённые заказы",
                     note="Исправление ошибочно принятых денег; выручку не меняет (D-18)."),
    A.REFUND: Mapping(None, OPERATING, "refunds", "Возвраты клиентам",
                      note="Выручку уменьшает сам возврат днём возврата."),
    A.SUPPLY: Mapping(None, OPERATING, "suppliers", "Оплата поставщикам за материал"),
    # Старые расходы, внесённые прямо в кассу (с 27.09.2026 так нельзя): у них
    # нет «за какой месяц», в ОПиУ они идут днём оплаты — иначе выпали бы вовсе.
    A.EXPENSE: Mapping(OPEX_CASH_MANUAL, OPERATING, "expense_manual",
                       "Расходы цеха (внесены в кассе)"),
    A.SALARY: Mapping(OPEX_CASH_MANUAL, OPERATING, "salary_manual",
                      "Зарплата (внесена в кассе)"),
    A.COUNT: Mapping(CASH_COUNT, OPERATING, "cash_count", "Недостача / излишек кассы",
                     note="Пропавшие или лишние деньги — и поток, и прибыль (D-5)."),
    A.OTHER: Mapping(None, OPERATING, "other", "Прочее",
                     note="Без статьи: в прибыль не идёт, в сверке — своей строкой."),
    A.DEPOSIT: Mapping(None, FINANCING, "owner_in", "Вложения владельца"),
    A.OWNER_OUT: Mapping(None, FINANCING, "owner_out", "Изъятия владельца"),
    A.LOAN_IN: Mapping(None, FINANCING, "loan_in", "Получено займов"),
    A.LOAN_OUT: Mapping(None, FINANCING, "loan_out", "Погашено займов (тело)",
                        note="Только тело займа; проценты — тратой вида «Проценты по займам»."),
    A.TRANSFER: Mapping(None, OUTSIDE, "transfer", "Переводы между кассой и банком",
                        note="Свои деньги переложили — итог не меняется (D-6)."),
    A.OPENING: Mapping(None, OUTSIDE, "opening", "Ввод начального остатка",
                       note="Деньги, бывшие до учёта в системе, — не движение (D-5)."),
    # Аванс и расчёт по ведомости: расход на зарплату уже начислен за месяц
    # (`PayrollAccrual` → трата без денег), здесь только деньги.
    A.PAYROLL: Mapping(None, OPERATING, "payroll", "Выплата зарплаты по ведомости",
                       note="Зарплата в ОПиУ — начислением за месяц; деньги — выплатами (D-100)."),
}

# Системные строки ОПиУ — считаются, а не вносятся. В ОДДС их нет: деньги по
# выручке приходят оплатами, по себестоимости — уходили закупом.
SYSTEM = {
    REVENUE: Mapping(REVENUE, None, note="По дате признания выручки (D-8, D-14), возврат — днём возврата."),
    COGS_MATERIAL: Mapping(COGS_MATERIAL, None, note="FIFO-снимок в момент продажи."),
    COGS_SERVICES: Mapping(COGS_SERVICES, None),
    COGS_WARRANTY: Mapping(COGS_WARRANTY, None,
                           note="Себестоимость заказов-переделок за счёт цеха (Receipt.is_warranty)."),
    LOSSES: Mapping(LOSSES, None, note="Брак и недостача по себестоимости (D-15)."),
    TAX: Mapping(TAX, None, note="Ставка истории TaxRate × выручка месяца (D-10, D-19)."),
    DEPRECIATION: Mapping(DEPRECIATION, None),
    DISPOSAL: Mapping(DISPOSAL, None, note="Остаток стоимости в месяц выбытия (D-20)."),
}


def for_role(role) -> Mapping:
    return ROLES[role]


def for_expense(entry) -> Mapping:
    """Трата «Финансов»: по роли вида, капвложение — с учётом порога."""
    role = entry.kind.role
    if entry.asset_id:
        return ASSET_PAYMENT
    if role == Role.CAPEX and not entry.is_capitalized:
        return CAPEX_EXPENSED
    return ROLES[role]


def for_article(article) -> Mapping:
    return ARTICLES[article]


def for_cash_entry(entry) -> Mapping:
    """Запись кассы: привязанная к трате — по трате, остальные — по статье."""
    if entry.expense_id:
        return for_expense(entry.expense)
    return ARTICLES.get(entry.article, ARTICLES[A.OTHER])
