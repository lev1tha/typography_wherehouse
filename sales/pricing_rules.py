"""Правила прайса (2026-10-10, CALC-01, CLI-02, CALC-08): минимальная сумма
строки, наценка за срочность и скидка клиента.

Применяются ПОСТРОЧНО при сборке строки чека, в одном порядке:

    расчёт по каталогу (или вписанной цене)
      → минимум строки (только у услуг): строка дешевле — стоит минимум
      → × (1 + срочность %)
      → × (1 − скидка %)
      → вверх до целого сома (прежнее правило строки)

Итог чека остаётся суммой строк, а строка — `количество × price_per_item`
(вверх до сома), как и раньше. Поэтому долг, касса, ОПиУ/ОДДС, маржа и все
отчёты, которые складывают строки, согласованы без правок: правило меняет
только цену за единицу, по которой строку записали. Из чего эта цена вышла —
в полях строки (`catalog_price`, `min_amount`, `min_applied`,
`urgency_percent`, `discount_percent`).

Все правила по умолчанию выключены (0) — тогда цена строки равна
каталожной до копейки, и поведение прежнее.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")
SOM = Decimal("1")


def _ceil_s(value: Decimal) -> Decimal:
    """Вверх до целого сома, не ниже нуля."""
    return max(Decimal(value).quantize(SOM, rounding=ROUND_CEILING), ZERO)


@dataclass(frozen=True)
class LineRules:
    """Правила одной строки: действующий минимум (0 — нет), срочность и скидка, %."""

    minimum: Decimal = ZERO
    urgency: Decimal = ZERO
    discount: Decimal = ZERO

    @property
    def is_noop(self) -> bool:
        return self.minimum <= 0 and self.urgency == 0 and self.discount == 0


@dataclass(frozen=True)
class Breakdown:
    """Раскладка строки по правилам — для отчёта «сколько дали и взяли»."""

    catalog: Decimal  # количество × цена до правил, до копейки
    minimum: Decimal  # сколько добавил минимум
    urgency: Decimal  # сколько добавила срочность
    discount: Decimal  # сколько сняла скидка (положительное число)


def exact_total(qty: Decimal, base_price: Decimal, rules: LineRules) -> tuple[Decimal, bool]:
    """Стоимость строки по правилам БЕЗ округления до сома и признак «сработал
    минимум». Минимум не трогает строку за 0: нулевая цена — осознанный подарок
    админа, а не «слишком дешёвая работа»."""
    amount = qty * base_price
    applied = False
    if rules.minimum > 0 and ZERO < amount < rules.minimum:
        amount = rules.minimum
        applied = True
    amount = amount * (HUNDRED + rules.urgency) / HUNDRED
    amount = amount * (HUNDRED - rules.discount) / HUNDRED
    return amount, applied


def target_total(qty: Decimal, base_price: Decimal, rules: LineRules) -> tuple[Decimal, bool]:
    """Стоимость строки по правилам (целые сомы, вверх) и признак «сработал минимум»."""
    amount, applied = exact_total(qty, base_price, rules)
    return _ceil_s(amount), applied


def price_for_target(qty: Decimal, target: Decimal) -> Decimal:
    """Наибольшая цена за единицу (до копейки), при которой `количество × цена`,
    округлённое вверх до сома, равно целевой сумме. При количестве до 100 единиц
    попадание точное; при большем строка может выйти на несколько сомов дороже
    цели, но никогда не дешевле."""
    qty = Decimal(qty)
    target = Decimal(target)
    if qty <= 0:
        return ZERO
    price = (target / qty).quantize(CENT, rounding=ROUND_FLOOR)
    if _ceil_s(qty * price) < target:
        price += CENT
    return price


def price_for(qty: Decimal, base_price: Decimal, rules: LineRules) -> tuple[Decimal, bool]:
    """Цена за единицу (до копейки), при которой строка стоит ровно столько,
    сколько велят правила, и признак «сработал минимум».

    Строка хранит не сумму, а цену за единицу (`количество × цена`, вверх до
    сома) — так её читают все отчёты. Подбираем наибольшую цену с копейками, у
    которой строка выходит ровно в целевую сумму. Это всегда возможно, пока
    количество не больше 100 единиц; при большем количестве шаг в копейку
    двигает строку больше чем на сом, и строка может выйти на несколько сомов
    дороже цели (никогда — дешевле).
    """
    qty = Decimal(qty)
    base_price = Decimal(base_price)
    if rules.is_noop or qty <= 0:
        return base_price, False
    target, applied = target_total(qty, base_price, rules)
    if _ceil_s(qty * base_price) == target:
        return base_price, applied
    return price_for_target(qty, target), applied


def allocate_order_total(exacts: list[Decimal]) -> list[Decimal]:
    """Раскладка итога заказа по строкам (режим «итог одной формулой»).

    Итог — сумма точных стоимостей строк, вверх до целого сома. Каждая строка
    получает свою стоимость вниз до сома, а разница округления (меньше числа
    строк плюс один сом) целиком ложится в ПОСЛЕДНЮЮ строку с ненулевой
    стоимостью. Так строки остаются целыми сомами, их сумма равна итогу, а
    каждая отличается от точной не больше чем на число строк сомов. Строки за
    0 (подарок) остаются нулевыми.
    """
    if not exacts:
        return []
    total = _ceil_s(sum(exacts, ZERO))
    floors = [max(e, ZERO).quantize(SOM, rounding=ROUND_FLOOR) for e in exacts]
    rest = total - sum(floors, ZERO)
    shares = list(floors)
    if rest > 0:
        for i in range(len(exacts) - 1, -1, -1):
            if exacts[i] > 0:
                shares[i] += rest
                break
    return shares


def breakdown(item) -> Breakdown | None:
    """Раскладка проданной строки: каталог, + минимум, + срочность, − скидка.

    Считается от записанных на строке правил, до копейки (без округления
    строки до сома — оно своё у каждой строки и в отчёт «скидок» не входит).
    `None` — строка продана до правил.
    """
    if item.catalog_price is None:
        return None
    catalog = item.quantity * item.catalog_price
    after_min = catalog
    if item.min_applied and item.min_amount:
        after_min = max(item.min_amount, catalog)
    urgency_pct = item.urgency_percent or ZERO
    discount_pct = item.discount_percent or ZERO
    urgency = after_min * urgency_pct / HUNDRED
    discount = (after_min + urgency) * discount_pct / HUNDRED
    q = lambda v: Decimal(v).quantize(CENT)  # noqa: E731
    return Breakdown(
        catalog=q(catalog), minimum=q(after_min - catalog), urgency=q(urgency), discount=q(discount),
    )


def rules_summary(d_from=None, d_to=None) -> dict:
    """Сколько за период взяли минимумом и срочностью и сколько отдали скидкой.

    Строки продаж периода (день признания выручки, как во всех отчётах),
    кроме возвращённых и отменённых чеков. Деньги отчётов при этом уже
    посчитаны правильно — выручка и так сумма строк по цене после правил;
    это лишь раскладка «откуда разница с каталогом».
    """
    from .models import TransactionItem
    from .reporting import LINE_SOLD_ON, _between, sold_lines

    lines = _between(
        sold_lines(TransactionItem.objects.filter(is_returned=False, catalog_price__isnull=False)),
        LINE_SOLD_ON, d_from, d_to,
    ).only(
        "quantity", "price_per_item", "catalog_price", "min_amount", "min_applied",
        "urgency_percent", "discount_percent", "receipt_id",
    )
    totals = {"minimum": ZERO, "urgency": ZERO, "discount": ZERO}
    receipts = {"minimum": set(), "urgency": set(), "discount": set()}
    lines_count = {"minimum": 0, "urgency": 0, "discount": 0}
    for item in lines:
        parts = breakdown(item)
        for key in totals:
            value = getattr(parts, key)
            if value > 0:
                totals[key] += value
                receipts[key].add(item.receipt_id)
                lines_count[key] += 1
    return {
        key: {"amount": totals[key], "orders": len(receipts[key]), "lines": lines_count[key]}
        for key in totals
    }
