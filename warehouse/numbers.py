"""Числа, как их пишет русский Excel (аудит XL-01).

«1,22», «2 679», «2 679,50» с неразрывным пробелом в разрядах, «2679 сом» —
DRF на всё это отвечает «Требуется численное значение», и вставка пачки из
таблицы владельца падала целиком. Здесь — одна нормализация текста в число
для сериализаторов склада; та же логика на фронте — `utils/pasteTable.js`
(`parseNumber`) плюс отрезание «сом» в сетке каталога.
"""
from __future__ import annotations

import re

_SPACES = re.compile(r"[\s    ']")
_UNIT = re.compile(r"(?i)(сомов|сома|сом|som|kgs|₸)\.?$")


def normalize_number_text(value):
    """Текст ячейки → строка числа с точкой. Не строка — как есть.

    Пробелы (обычные, неразрывные, узкие, апостроф Excel) убираются, «сом» в
    конце отрезается, запятая становится точкой. Если в числе и точка, и
    запятая — разделитель целой части тот, что стоит последним
    («1.234,56» → 1234.56, «1,234.56» → 1234.56). Что не похоже на число,
    остаётся как было — сериализатор объяснит ошибку по-человечески.
    """
    if not isinstance(value, str):
        return value
    text = _SPACES.sub("", value)
    text = _UNIT.sub("", text)
    if not text:
        return ""
    comma, dot = text.rfind(","), text.rfind(".")
    if comma >= 0 and dot >= 0:
        if comma > dot:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif comma >= 0:
        if text.count(",") > 1:
            return value            # «1,2,3» — не число, пусть сериализатор скажет
        text = text.replace(",", ".")
    if not re.fullmatch(r"[+-]?\d*\.?\d+", text):
        return value
    return text


def normalize_numbers(data, fields):
    """Копия словаря `data`, где значения `fields` прошли нормализацию."""
    if not hasattr(data, "items"):
        return data
    # QueryDict (форма, а не JSON) — копией самого QueryDict: `dict()` от него
    # превратил бы каждое значение в список.
    out = data.copy() if hasattr(data, "setlist") else dict(data)
    for name in fields:
        if name in out:
            out[name] = normalize_number_text(out[name])
    return out
