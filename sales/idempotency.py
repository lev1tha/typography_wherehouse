"""Ключ повтора запроса (`Idempotency-Key`) для оформления чека и приёма оплаты.

Касса на плохой сети повторяет запрос, не дождавшись ответа, — и без ключа
получается второй чек (или вторая оплата). Фронт шлёт заголовок
`Idempotency-Key: <uuid>`; повтор с тем же ключом от того же пользователя
возвращает результат первого раза и ничего не создаёт. Ключ необязателен: без
заголовка всё работает как раньше.

Запись ключа делается В ТОЙ ЖЕ транзакции, что и сама операция: упала операция —
откатился и ключ, повтор пройдёт как первый. Два одновременных запроса с одним
ключом упираются в unique-ограничение: второй ждёт первого и получает его
результат.
"""
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import IdempotencyRecord

HEADER = "Idempotency-Key"
KEY_MAX_LENGTH = 100
KEEP_FOR = timedelta(days=7)
_KEY_RE = re.compile(r"^[\w.:\-]+$")


class InvalidKey(ValueError):
    """Заголовок есть, но ключ непригоден (пустой, слишком длинный, мусор)."""


def key_from(request):
    """Ключ из заголовка запроса или None, если заголовка нет."""
    raw = request.headers.get(HEADER)
    if raw is None:
        return None
    key = raw.strip()
    if not key or len(key) > KEY_MAX_LENGTH or not _KEY_RE.match(key):
        raise InvalidKey(
            f"Заголовок {HEADER} должен быть непустой строкой до {KEY_MAX_LENGTH} "
            "символов (буквы, цифры, «-», «_», «.», «:») — например, uuid."
        )
    return key


@transaction.atomic
def claim(user, endpoint, key, *, response_status):
    """Занять ключ. Возвращает `(запись, повтор)`.

    ``повтор=True`` — ключ уже использовали: операцию повторять нельзя, нужно
    отдать результат первой (`запись.receipt`). Иначе запись только что создана,
    и вызывающий обязан проставить в неё `receipt` до конца транзакции.
    Записи старше недели чистятся здесь же, лениво.
    """
    IdempotencyRecord.objects.filter(created_at__lt=timezone.now() - KEEP_FOR).delete()
    lookup = {"user": user, "endpoint": endpoint, "key": key}
    existing = IdempotencyRecord.objects.filter(**lookup).first()
    if existing is not None:
        return existing, True
    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.create(
                response_status=response_status, **lookup
            )
    except IntegrityError:
        # Параллельный запрос с тем же ключом успел раньше и уже закоммитился.
        existing = IdempotencyRecord.objects.filter(**lookup).first()
        if existing is None:
            raise
        return existing, True
    return record, False
