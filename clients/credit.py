"""Лимит долга клиента и предупреждения для кассы (CLI-03, D-92).

Лимит ничего не запрещает: при отгрузке в долг сверх лимита касса получает
предупреждение `debt_over_limit`, и решает человек. Всё выключено, пока владелец
не задал значение: у клиента `credit_limit` пуст, общий лимит
(`ClientSettings.default_credit_limit`) пуст, давность
(`PricingSettings.debt_warn_days`) 0.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

ZERO = Decimal("0")


def client_debt_now(client) -> Decimal:
    """Долг клиента сейчас — той же функцией, что карточка (`client_debt`):
    чеки по формуле `Receipt.debt` плюс входящий долг (волна 2)."""
    from .serializers import client_debt

    return client_debt(client)


def warn_days() -> int:
    """Давность долга для предупреждения, дней (0 — не предупреждать).

    Живёт в настройках цен — её читает и касса; здесь только чтение.
    """
    try:
        from services.models import PricingSettings

        return int(getattr(PricingSettings.load(), "debt_warn_days", 0) or 0)
    except Exception:   # настройки цен недоступны — предупреждение по давности выключено
        return 0


def credit_warnings(client, extra=ZERO) -> list[dict]:
    """Предупреждения о долге, если отгрузить клиенту ещё на `extra` сом в долг.

    `extra` — часть НОВОГО заказа, которая останется неоплаченной. Заказ,
    оплаченный целиком, риска не несёт — предупреждений нет.
    """
    extra = Decimal(str(extra or 0))
    if client is None or extra <= 0:
        return []
    from .opening import opening_oldest

    debt = client_debt_now(client)
    oldest = None
    for r in client.receipts.all():
        if r.debt > 0:
            day = timezone.localtime(r.revenue_recognized_at).date()
            oldest = day if oldest is None or day < oldest else oldest
    # Входящий долг (волна 2) стареет с даты переезда.
    opening_at = opening_oldest(client)
    if opening_at is not None:
        day = timezone.localtime(opening_at).date()
        oldest = day if oldest is None or day < oldest else oldest
    out = []
    limit = client.effective_credit_limit
    if limit is not None and debt + extra > limit:
        own = client.credit_limit is not None
        out.append({
            "code": "debt_over_limit", "reason": "limit",
            "source": "client" if own else "default",
            "limit": limit, "debt": debt, "debt_after": debt + extra,
            "message": f"Долг клиента после заказа {debt + extra} сом выше лимита {limit} сом.",
        })
    days = warn_days()
    if days and oldest is not None:
        age = (timezone.localdate() - oldest).days
        if age > days:
            out.append({
                "code": "debt_over_limit", "reason": "age", "days": age, "warn_days": days,
                "debt": debt, "debt_after": debt + extra,
                "message": f"У клиента уже есть долг {debt} сом, ему {age} дн. (порог {days} дн.).",
            })
    return out
