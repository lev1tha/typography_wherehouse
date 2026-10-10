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
           happened_on=None, receipt=None, supply=None, expense=None, roll=None,
           note="", user=None, is_auto=True):
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
        expense=expense,
        roll=roll,
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


def held_by_account(receipt) -> dict:
    """Сколько денег по этому чеку лежит на каждом счёте: приходы минус расходы
    его записей. {"CASH": 300, "BANK": 507}."""
    from .models import CashEntry

    held = {}
    for account, kind, amount in CashEntry.objects.filter(receipt=receipt).values_list(
        "account", "kind", "amount"
    ):
        sign = 1 if kind == CashEntry.Kind.IN else -1
        held[account] = held.get(account, Decimal("0")) + sign * amount
    return held


def take_back(receipt, amount, article, *, user=None, note=""):
    """Вернуть деньги по чеку — С ТЕХ СЧЕТОВ, куда они по нему пришли.

    Раньше расход писался по способу оплаты ЧЕКА. Но долг часто гасят не тем
    способом, которым оформляли заказ: заказ «наличными» закрыли переводом на
    507, откатили оплату — и наличные ушли в −507, а банк остался с деньгами,
    которых там уже нет. Разошлись оба счёта сразу.

    Берём сначала со счёта способа чека (если по чеку там есть деньги), потом
    с остальных, где больше. Не хватило (старые чеки без записей в книге) —
    остаток по способу чека, как было.
    """
    left = Decimal(str(amount or 0))
    if left <= 0:
        return []
    own = account_for(receipt.payment_method)
    held = held_by_account(receipt)
    order = sorted(held.items(), key=lambda kv: (kv[0] != own, -kv[1]))
    entries = []
    for account, value in order:
        if left <= 0:
            break
        take = min(value, left)
        if take > 0:
            entries.append(money_out(
                take, article, account=account, receipt=receipt, user=user, note=note,
            ))
            left -= take
    if left > 0:
        entries.append(money_out(
            left, article, account=own, receipt=receipt, user=user, note=note,
        ))
    return entries


def refund_paid(receipt, amount, *, user=None):
    """Возврат клиенту — деньги уходят с того счёта, куда пришли."""
    from .models import CashEntry

    return take_back(
        receipt, amount, CashEntry.Article.REFUND, user=user,
        note=f"Возврат по заказу №{receipt.order_number}" if receipt.order_number else "",
    )


def payment_reverted(receipt, amount, *, user=None, note=None):
    """Откат ошибочно принятой оплаты.

    Не стираем приход, а пишем встречный расход: кассовая книга не подчищается
    задним числом, иначе по ней нельзя объяснить, что происходило. Расход — с
    того счёта, куда деньги по этому чеку пришли.
    """
    from .models import CashEntry

    if note is None:
        note = (
            f"Откат оплаты по заказу №{receipt.order_number}" if receipt.order_number else ""
        )
    return take_back(receipt, amount, CashEntry.Article.UNPAY, user=user, note=note)


def receipt_deleted(receipt, *, user=None):
    """Заказ удалили — деньги, лежавшие по нему в кассе, уходят встречной
    записью на каждом счёте. Сами записи остаются в книге (ссылка на чек
    обнулится, номер заказа — в примечании): книга не подчищается, иначе
    остаток сойдётся, а объяснить его будет нечем."""
    from .models import CashEntry

    label = f"№{receipt.order_number}" if receipt.order_number else "без номера"
    note = f"Заказ {label} удалён"
    for account, held in held_by_account(receipt).items():
        if held > 0:
            money_out(held, CashEntry.Article.UNPAY, account=account,
                      receipt=receipt, user=user, note=note)
        elif held < 0:
            money_in(-held, CashEntry.Article.UNPAY, account=account,
                     receipt=receipt, user=user, note=note)


def supplier_paid(amount, account, *, roll=None, supply=None, happened_on=None,
                  note="", user=None):
    """Заплатили поставщику за материал — деньги ушли.

    Зовут её только тогда, когда человек ПРИ ПРИЁМКЕ выбрал счёт: система не
    может знать, отдали за поставку деньги или взяли в долг, а догадка здесь
    дороже пропуска. Не выбрали — записи нет, и касса остаётся как была.

    Ноль и минус `record` отсекает сам: накладная без оплаты (взяли в долг)
    сюда дойдёт с нулём и ничего не запишет.
    """
    from .models import CashEntry

    return money_out(
        amount, CashEntry.Article.SUPPLY,
        account=account,
        happened_on=happened_on,
        roll=roll, supply=supply, note=note, user=user,
    )


def supplier_refund(amount, account, *, supply=None, happened_on=None, note="", user=None):
    """Поставщик вернул деньги (за возвращённый товар) — приход той же статьёй
    «Оплата поставщику»: в ОДДС это уменьшение оплаты поставщикам, а не
    выручка. Зовёт «Вернуть поставщику» (`warehouse.supplier_returns`)."""
    from .models import CashEntry

    return money_in(
        amount, CashEntry.Article.SUPPLY, account=account, happened_on=happened_on,
        supply=supply, note=note, user=user,
    )


def sync_expense(entry, *, user=None):
    """Привести кассовую запись траты в соответствие с самой тратой.

    Одна функция на создание и на правку: трату правят чаще, чем заводят
    (сумму уточнили, дату сдвинули, счёт перепутали), и две почти одинаковые
    ветки разошлись бы на первой же доработке. Запись у траты всегда одна —
    старые убираем, новую пишем.

    Вид с ролью «без денег» («Долг материала») в кассу не идёт: эта запись
    означает «материал взяли, деньги ещё не отдали», и расхода по ней не было.
    Остальные виды — реальные деньги, ушедшие из ящика или со счёта, включая
    вложения: станок за 300 000 в прибыль идёт амортизацией, а из кассы уходит
    целиком в день покупки.
    """
    from .models import CashEntry, ExpenseKind

    # Начисление зарплаты и карточка актива в рассрочку — записи БЕЗ денег; с
    # ними не только нечего писать, но и нельзя стирать: к начислению
    # привязаны выплаты (они в кассе своей статьёй), к активу — платежи.
    if entry.is_cashless:
        return None
    entry.cash_entries.all().delete()
    if not entry.kind.moves_cash:
        return None
    article = (
        CashEntry.Article.SALARY
        if entry.kind.code == ExpenseKind.SALARY
        else CashEntry.Article.EXPENSE
    )
    return money_out(
        entry.amount, article,
        account=entry.account,
        happened_on=entry.spent_at,
        note=f"{entry.kind.name}: {entry.name}".strip(": ") or entry.kind.name,
        user=user or entry.created_by,
        expense=entry,
    )


def reverse_supplier_payments(*, roll=None, supply=None, note="", user=None):
    """Отменили приход или накладную — вернуть их оплату встречной записью.

    Раньше оплату стирали: партия уносила её каскадом, отмена накладной —
    явным удалением. Деньги поставщику в прошлом месяце исчезали из книги, и
    ОДДС уже принятого месяца менялся без единой строки (аудит, Б-13). Теперь
    исходная запись остаётся, а на каждом счёте, где по документу ушли деньги,
    пишется приход той же статьёй сегодняшним днём: «поставки не было, деньги
    вернули» (или зачли поставщику — для кассы это одно и то же).

    Берём только записи системы (`is_auto`): ручную запись человек сделал сам,
    ему и решать, что с ней делать. Вызывать ДО удаления партии/накладной.
    """
    from .models import CashEntry

    if roll is None and supply is None:
        return []
    qs = CashEntry.objects.filter(is_auto=True, article=CashEntry.Article.SUPPLY)
    qs = qs.filter(roll=roll) if roll is not None else qs.filter(supply=supply)
    held = {}
    for account, kind, amount in qs.values_list("account", "kind", "amount"):
        sign = -1 if kind == CashEntry.Kind.OUT else 1
        held[account] = held.get(account, Decimal("0")) + sign * amount
    entries = []
    for account, value in held.items():
        if value < 0:
            entries.append(money_in(-value, CashEntry.Article.SUPPLY, account=account,
                                    roll=roll, supply=supply, note=note, user=user))
        elif value > 0:
            entries.append(money_out(value, CashEntry.Article.SUPPLY, account=account,
                                     roll=roll, supply=supply, note=note, user=user))
    return entries


def balances_after() -> dict:
    """{id записи: остаток её счёта сразу после неё} по всей книге.

    Порядок — как человек читает книгу: по дате операции, внутри дня по
    времени ввода (cash-05). Остаток считается по ВСЕЙ истории счёта, а не по
    выбранной странице: фильтр списка баланса не меняет."""
    from .models import CashEntry

    running = {}
    out = {}
    rows = CashEntry.objects.order_by("happened_on", "created_at", "id").values_list(
        "id", "account", "kind", "amount"
    )
    for entry_id, account, kind, amount in rows:
        value = running.get(account, Decimal("0")) + (amount if kind == CashEntry.Kind.IN else -amount)
        running[account] = value
        out[entry_id] = value
    return out
