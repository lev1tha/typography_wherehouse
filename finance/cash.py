"""Запись движений денег в кассовую книгу.

Отдельный модуль, а не метод модели: вызывают его из `sales.sale_service` — из
кода, который и так делает три вещи разом, — и там важно, чтобы вызов читался
одной строкой и НИКОГДА не ронял продажу. Касса — учётная надстройка: если она
почему-то не записалась, деньги от этого не исчезли и чек оформиться обязан.
"""
from __future__ import annotations

from decimal import Decimal


def account_for(payment_method) -> str:
    """Каким счётом легли деньги: наличные в ящик, остальное — в банк.

    MBank и DemirBank — переводы, в кассе их нет; складывать их с наличными
    значит получить остаток, которого в ящике не окажется.
    """
    from .models import CashEntry

    return (
        CashEntry.Account.CASH
        if str(payment_method) == "CASH"
        else CashEntry.Account.BANK
    )


def record(kind, amount, article, *, account=None, payment_method=None,
           happened_on=None, receipt=None, supply=None, note="", user=None,
           is_auto=True):
    """Записать движение. Ноль и минус игнорируем — это не операция."""
    from .models import CashEntry

    value = Decimal(str(amount or 0))
    if value <= 0:
        return None
    return CashEntry.objects.create(
        account=account or account_for(payment_method),
        kind=kind,
        article=article,
        amount=value,
        **({"happened_on": happened_on} if happened_on else {}),
        receipt=receipt,
        supply=supply,
        note=note,
        created_by=user,
        is_auto=is_auto,
    )


def money_in(amount, article, **kwargs):
    from .models import CashEntry

    return record(CashEntry.Kind.IN, amount, article, **kwargs)


def money_out(amount, article, **kwargs):
    from .models import CashEntry

    return record(CashEntry.Kind.OUT, amount, article, **kwargs)


def receipt_paid(receipt, amount, *, user=None, happened_on=None, method=None):
    """Клиент заплатил — деньги пришли."""
    from .models import CashEntry

    return money_in(
        amount, CashEntry.Article.SALE,
        payment_method=method or receipt.payment_method,
        happened_on=happened_on, receipt=receipt, user=user,
        note=f"Заказ №{receipt.order_number}" if receipt.order_number else "",
    )


def change_given(receipt, amount, *, user=None):
    """Сдачу отдали на руки — деньги ушли, и всегда наличными."""
    from .models import CashEntry

    return money_out(
        amount, CashEntry.Article.CHANGE,
        account=CashEntry.Account.CASH,
        receipt=receipt, user=user,
        note=f"Сдача по заказу №{receipt.order_number}" if receipt.order_number else "",
    )


def refund_paid(receipt, amount, *, user=None):
    """Возврат клиенту — деньги ушли тем же путём, каким пришли."""
    from .models import CashEntry

    return money_out(
        amount, CashEntry.Article.REFUND,
        payment_method=receipt.payment_method,
        receipt=receipt, user=user,
        note=f"Возврат по заказу №{receipt.order_number}" if receipt.order_number else "",
    )


def payment_reverted(receipt, amount, *, user=None):
    """Откат ошибочно принятой оплаты.

    Не стираем приход, а пишем встречный расход: кассовая книга не подчищается
    задним числом, иначе по ней нельзя объяснить, что происходило.
    """
    from .models import CashEntry

    return money_out(
        amount, CashEntry.Article.UNPAY,
        payment_method=receipt.payment_method,
        receipt=receipt, user=user,
        note=f"Откат оплаты по заказу №{receipt.order_number}" if receipt.order_number else "",
    )
