"""Область одного отчёта: общие справочные данные грузятся один раз.

Годовая таблица ОПиУ — это 12 вызовов `pnl()`, и каждый заново читал одни и те
же таблицы: ставки налога, траты, амортизируемые покупки, всю кассовую книгу
для остатков. Это сотни запросов на экран.

`@report_scope` на верхнем отчёте открывает область; всё, что внутри (вложенные
отчёты, месячные вызовы), берёт данные через `once(ключ, загрузка)` — первый раз
из базы, дальше из памяти. Вне области (тесты, shell, одиночный вызов) `once`
просто загружает заново, поэтому правки данных между вызовами видны сразу.
Область живёт ровно один вызов верхнего отчёта и в память процесса ничего не
оставляет. Кэшированные объекты только читаются, не меняются.
"""
from contextvars import ContextVar
from functools import wraps

_SCOPE: ContextVar = ContextVar("finance_report_scope", default=None)


def report_scope(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        if _SCOPE.get() is not None:      # вложенный отчёт — общая область
            return fn(*args, **kwargs)
        token = _SCOPE.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            _SCOPE.reset(token)

    return inner


def once(key, load):
    scope = _SCOPE.get()
    if scope is None:
        return load()
    if key not in scope:
        scope[key] = load()
    return scope[key]
