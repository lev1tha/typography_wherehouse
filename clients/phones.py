"""Поиск клиента по телефону, устойчивый к тому, КАК номер записали.

Номер хранится ровно так, как его набрали в кассе, — и это правильно: заказчик
привык видеть его в своём формате. Но искать по строке нельзя: один и тот же
человек записывается то как `+996555111222`, то как `0555 111 222`, то как
`(555) 11-12-22`. Строки разные, `filter(phone=...)` не находит совпадения, и
касса заводит НОВОГО клиента. Дальше его заказы и долг живут двумя стопками под
двумя карточками — «Тахир ака» и «ака Тахир» оказываются разными людьми.

Сравниваем поэтому не строки, а цифры.
"""
import re

from .models import Client

# Мобильный в Кыргызстане — 9 цифр после кода страны (555 11 12 22). По ним и
# опознаём номер: код страны пишут то `+996`, то `996`, то `0`, то опускают.
LOCAL_LENGTH = 9


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def phone_key(value: str) -> str:
    """Хвост номера, по которому его узнаём. Короткие номера берём целиком."""
    digits = only_digits(value)
    return digits[-LOCAL_LENGTH:] if len(digits) >= LOCAL_LENGTH else digits


def find_client_by_phone(phone: str, queryset=None):
    """Клиент с этим номером в ЛЮБОЙ записи, или None.

    Сначала ищем точное совпадение цифр и только потом — по последним девяти.
    Порядок важен: у двух разных номеров могут совпасть хвосты (городской и
    мобильный), и брать первый попавшийся нельзя — точное совпадение всегда
    вернее.

    Фильтруем в Python, а не запросом: убрать разделители средствами БД
    переносимо между SQLite и PostgreSQL не получится, а клиентов у цеха
    сотни — не тот объём, ради которого стоит городить SQL.
    """
    digits = only_digits(phone)
    if not digits:
        return None

    clients = list(queryset if queryset is not None else Client.objects.all())
    exact = next((c for c in clients if only_digits(c.phone) == digits), None)
    if exact:
        return exact

    key = phone_key(phone)
    if len(key) < LOCAL_LENGTH:
        # Слишком короткий огрызок совпал бы со слишком многим.
        return None
    return next((c for c in clients if phone_key(c.phone) == key), None)
