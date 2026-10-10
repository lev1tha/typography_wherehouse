"""Дебиторка и аналитика клиентов: давность долга, сальдо, маржа, «спящие».

Одна формула долга на все экраны — та же, что `Receipt.debt` (`sales/models.py`):
чек в статусах `OWING_STATUSES`, не отменён, с признанной выручкой (D-7/D-37) и
итогом больше «оплачено + возвращено». Здесь она записана условием для
запросов (`owing_q`) и ровно так же, как раньше, в списке клиентов — для
сортировок и фильтров на уровне БД. Любое расхождение ловит тест
`tests_debt_age` (сумма корзин = долг карточек).

Сальдо клиента = долг − сдача − аванс: плюс — он должен нам, минус — мы ему.

Входящий долг на дату переезда из Excel (волна 2, `clients.opening`) — часть
долга: в давности он стоит датой переезда, в корзинах — отдельной «строкой»
(как заказ), в фильтрах возраста — наравне с заказами.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import (
    Case,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    Max,
    Min,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Least
from django.utils import timezone

ZERO = Decimal("0")
MONEY = DecimalField(max_digits=14, decimal_places=2)

# Корзины давности долга, дней (включительно). Последняя — без верхней границы.
BUCKETS = (("0_30", 0, 30), ("31_60", 31, 60), ("61_90", 61, 90), ("90_plus", 91, None))


def owing_q(prefix: str = "") -> Q:
    """Условие «по этому чеку клиент должен» — записью для запросов."""
    from sales.models import Receipt

    p = prefix
    return (
        Q(**{f"{p}payment_status__in": Receipt.OWING_STATUSES})
        & ~Q(**{f"{p}status": Receipt.Status.CANCELLED})
        & Q(**{f"{p}revenue_recognized_at__isnull": False})
        & Q(**{f"{p}total_price__gt": F(f"{p}amount_paid") + F(f"{p}refunded_amount")})
    )


def annotate_metrics(qs):
    """Аналитика клиента одним запросом: давность долга, аванс, сальдо, последний
    заказ и маржа. Дополняет аннотации списка (`debt`, `change_due_total`)."""
    from sales.models import Receipt, TransactionItem

    from .models import ClientAdvance
    from .opening import open_debts_qs

    live = ~Q(receipts__status=Receipt.Status.CANCELLED) & Q(receipts__revenue_recognized_at__isnull=False)
    opening_oldest = Subquery(
        open_debts_qs().filter(client=OuterRef("pk")).order_by("as_of_at").values("as_of_at")[:1]
    )
    receipts_oldest = Min(Case(When(owing_q("receipts__"), then=F("receipts__revenue_recognized_at"))))

    advances = (
        ClientAdvance.objects.filter(client=OuterRef("pk"), reverted_at__isnull=True)
        .order_by().values("client").annotate(v=Sum("remaining")).values("v")
    )
    revenue = (
        Receipt.objects.filter(client=OuterRef("pk"), revenue_recognized_at__isnull=False)
        .order_by().values("client")
        .annotate(v=Sum(F("total_price") - F("refunded_amount"), output_field=MONEY)).values("v")
    )
    cost = (
        TransactionItem.objects.filter(
            receipt__client=OuterRef("pk"), receipt__revenue_recognized_at__isnull=False,
            is_returned=False,
        )
        .order_by().values("receipt__client").annotate(v=Sum("cost_total")).values("v")
    )
    return qs.annotate(
        advance_total=Coalesce(Subquery(advances, output_field=MONEY), Value(ZERO), output_field=MONEY),
        # Самый старый неоплаченный заказ — по дате признания выручки (у
        # обычного заказа это дата заказа); входящий долг — датой переезда.
        # Least через Coalesce: на SQLite LEAST с NULL даёт NULL.
        oldest_debt_at=Least(
            Coalesce(receipts_oldest, opening_oldest),
            Coalesce(opening_oldest, receipts_oldest),
        ),
        last_order_at=Max(Case(When(live, then=F("receipts__revenue_recognized_at")))),
        margin_total=ExpressionWrapper(
            Coalesce(Subquery(revenue, output_field=MONEY), Value(ZERO), output_field=MONEY)
            - Coalesce(Subquery(cost, output_field=MONEY), Value(ZERO), output_field=MONEY),
            output_field=MONEY,
        ),
    ).annotate(
        balance=ExpressionWrapper(F("debt") - F("change_due_total") - F("advance_total"), output_field=MONEY),
    )


def local_date(moment) -> date | None:
    if moment is None:
        return None
    return timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()


def age_days(moment, today: date | None = None) -> int | None:
    """Сколько дней назад было событие (локальная дата), None — не было."""
    day = local_date(moment)
    if day is None:
        return None
    return max(0, ((today or timezone.localdate()) - day).days)


def metrics_for(client) -> dict:
    """То же, что `annotate_metrics`, для одной карточки — без аннотаций."""
    from .models import ClientAdvance

    oldest = last = None
    revenue = cost = ZERO
    for r in client.receipts.all():
        if r.revenue_recognized_at is None:
            continue
        revenue += r.total_price - r.refunded_amount
        cost += sum((i.cost_total for i in r.items.all() if not i.is_returned), ZERO)
        last = r.revenue_recognized_at if last is None or r.revenue_recognized_at > last else last
        if r.debt > 0:
            oldest = r.revenue_recognized_at if oldest is None or r.revenue_recognized_at < oldest else oldest
    advance = ClientAdvance.objects.filter(client=client, reverted_at__isnull=True).aggregate(
        v=Sum("remaining")
    )["v"] or ZERO
    from .opening import opening_oldest

    opening_at = opening_oldest(client)
    if opening_at is not None and (oldest is None or opening_at < oldest):
        oldest = opening_at
    return {
        "oldest_debt_at": oldest, "last_order_at": last,
        "margin_total": revenue - cost, "advance_total": advance,
    }


def filter_by_age(qs, *, overdue_days=None, age_from=None, age_to=None):
    """Клиенты, у которых есть неоплаченный заказ нужного возраста.

    `overdue_days=N` — самому старому долгу не меньше N дней (то же, что
    «oldest_debt_at ≤ сегодня − N»); `age_from/age_to` — есть заказ с долгом
    возраста в этом диапазоне (для плиток корзин).
    """
    from sales.models import Receipt

    from .opening import open_debts_qs

    if overdue_days is None and age_from is None and age_to is None:
        return qs
    today = timezone.localdate()
    owing = Receipt.objects.filter(owing_q(), client=OuterRef("pk"))
    opening = open_debts_qs().filter(client=OuterRef("pk"))
    low = age_from if age_from is not None else overdue_days
    if low is not None:
        # возраст ≥ low  <=>  заказ признан не позже, чем сегодня − low дней
        owing = owing.filter(revenue_recognized_at__date__lte=today - timedelta(days=low))
        opening = opening.filter(as_of__lte=today - timedelta(days=low))
    if age_to is not None:
        owing = owing.filter(revenue_recognized_at__date__gte=today - timedelta(days=age_to))
        opening = opening.filter(as_of__gte=today - timedelta(days=age_to))
    return qs.filter(Exists(owing) | Exists(opening))


def aging_buckets(today: date | None = None) -> dict:
    """Долг по возрасту заказа: 0–30 / 31–60 / 61–90 / больше 90 дней.

    Считается по заказам, а не по клиентам: один клиент может быть и в первой
    корзине, и в последней (как СУММЕСЛИМН по колонке «Дней» в Excel владельца).
    Долг без клиента (продажа «с улицы» в долг) идёт отдельной строкой — иначе
    сумма корзин не сошлась бы с плиткой долга в «Чеках».
    """
    from sales.models import Receipt

    today = today or timezone.localdate()
    out = {
        key: {"key": key, "from": low, "to": high, "amount": ZERO, "orders": 0, "clients": set()}
        for key, low, high in BUCKETS
    }
    no_client = {"amount": ZERO, "orders": 0}
    from .opening import open_debts_qs

    debts = list(Receipt.objects.filter(owing_q()).values_list(
        "client_id", "revenue_recognized_at", "total_price", "amount_paid", "refunded_amount"
    ))
    # Входящий долг на дату переезда (волна 2) — «заказом» с датой переезда.
    opening_total = ZERO
    for client_id, as_of_at, remaining in open_debts_qs().values_list("client_id", "as_of_at", "remaining"):
        debts.append((client_id, as_of_at, remaining, ZERO, ZERO))
        opening_total += remaining
    for client_id, recognized, total, paid, refunded in debts:
        owed = total - paid - refunded
        if owed <= 0:
            continue
        days = age_days(recognized, today) or 0
        for key, low, high in BUCKETS:
            if days >= low and (high is None or days <= high):
                bucket = out[key]
                bucket["amount"] += owed
                bucket["orders"] += 1
                if client_id:
                    bucket["clients"].add(client_id)
                else:
                    no_client["amount"] += owed
                    no_client["orders"] += 1
                break
    buckets = []
    for key, low, high in BUCKETS:
        b = out[key]
        buckets.append({**b, "clients": len(b["clients"])})
    return {
        "buckets": buckets,
        "total": sum((b["amount"] for b in buckets), ZERO),
        "no_client": no_client,
        # Из них — входящий долг на дату переезда (уже внутри корзин).
        "opening": opening_total,
    }
