"""Проверка суммы, которую принимают от клиента (RM-N9, D-186).

Аванс, общая выплата, списание долга и входящие остатки принимали любую
конечную сумму: телефон, вставленный в поле суммы, давал долг 996 млрд, а
«0,001» — тыйын, которого не бывает. Здесь одна проверка на все приёмы денег:
сумма меньше 10 000 000 000 сом и не больше двух знаков после запятой.
"""
from __future__ import annotations

from decimal import Decimal

MAX_AMOUNT = Decimal("10000000000")          # 10^10 сом — больше в цехе не бывает
CENT = Decimal("0.01")


class AmountRejected(ValueError):
    """Сумма не годится; текст уходит пользователю как есть."""


def check_amount(amount, what: str = "Сумма") -> Decimal | None:
    """Вернуть сумму, если она годится; иначе `AmountRejected`. None — как есть."""
    if amount is None:
        return None
    amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if not amount.is_finite():
        raise AmountRejected("Некорректная сумма.")
    if abs(amount) >= MAX_AMOUNT:
        raise AmountRejected(
            f"{what} слишком большая: должна быть меньше 10 000 000 000 сом. "
            "Проверьте, не попал ли в поле телефон или лишние нули."
        )
    if amount != amount.quantize(CENT):
        raise AmountRejected(f"{what}: не больше двух знаков после запятой (тыйыны).")
    return amount
