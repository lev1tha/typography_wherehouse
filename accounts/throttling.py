"""Пароли нельзя подбирать перебором.

Три двери системы открыты без токена — вход сотрудника, вход в кабинет клиента
и форма входа Django-админки. Предела попыток у них не было вовсе: пароль
кабинета выдаёт админ, он короткий, а портал живёт на публичном домене.

Считаем в двух разрезах: по адресу (обычный перебор с одной машины) и по самому
логину (перебор одного аккаунта с разных адресов).

Считаются только НЕУДАЧНЫЕ попытки. Попытка записывается в счётчик ещё до
проверки пароля (иначе пачка параллельных запросов проскочила бы под лимит,
пока ни один не закончился), а УДАЧНЫЙ вход эту запись снимает, а счётчик
аккаунта сбрасывает целиком. Так человек, который вошёл с первого раза, не
тратит лимит: ни свой, ни чужой.

Адрес клиента берётся по правилам DRF (`REST_FRAMEWORK["NUM_PROXIES"]`, в
настройках — `TRUSTED_PROXY_COUNT`). Без этой настройки заголовок
`X-Forwarded-For` берётся целиком и подделывается: каждый запрос с новой
подписью получает свой счётчик. С ней — берётся адрес, добавленный последним
доверенным прокси, а всё, что клиент приписал слева, игнорируется.
"""
import re
import time

from django.core.cache import cache
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle

from clients.phones import only_digits, phone_key


def client_ip(meta) -> str:
    """Адрес клиента — ровно как у `BaseThrottle.get_ident` (одно правило на всё)."""
    xff = meta.get("HTTP_X_FORWARDED_FOR")
    remote_addr = meta.get("REMOTE_ADDR")
    num_proxies = api_settings.NUM_PROXIES
    if num_proxies is not None:
        if num_proxies == 0 or xff is None:
            return remote_addr
        addrs = xff.split(",")
        return addrs[-min(num_proxies, len(addrs))].strip()
    return "".join(xff.split()) if xff else remote_addr


def account_key(username=None, phone=None):
    """Ключ аккаунта для счётчика: «Лазер», «лазер » и « ЛАЗЕР» — один и тот же.

    Телефон сводим к цифрам и последним девяти из них (`+996 555 11-12-22` и
    `0555111222` — один номер), иначе перебор менял бы написание и каждый раз
    получал чистый счётчик. Нестроковые значения (число, список) игнорируем —
    `.strip()` на них раньше ронял вход пятисотой.
    """
    if isinstance(phone, str) and only_digits(phone):
        return "p:" + phone_key(phone)
    name = username if isinstance(username, str) else None
    if name is None and isinstance(phone, str):
        name = phone
    name = re.sub(r"\s+", "", name or "").casefold()
    return ("u:" + name) if name else None


class Attempts:
    """Счётчик попыток в кеше: окно времени, предел, резервирование и снятие."""

    def __init__(self, scope, ident):
        rate = api_settings.DEFAULT_THROTTLE_RATES[scope]
        num, period = rate.split("/")
        self.limit = int(num)
        self.duration = {"s": 1, "m": 60, "h": 3600, "d": 86400}[period[0]]
        self.key = f"throttle_{scope}_{ident}"
        self.stamp = None

    def _history(self, now):
        history = cache.get(self.key, [])
        while history and history[-1] <= now - self.duration:
            history.pop()
        return history

    def take(self):
        """Записать попытку. True — пускаем, False — предел исчерпан."""
        now = time.time()
        history = self._history(now)
        if len(history) >= self.limit:
            self._wait = self.duration - (now - history[-1])
            return False
        history.insert(0, now)
        cache.set(self.key, history, self.duration)
        self.stamp = now
        return True

    def wait(self):
        return getattr(self, "_wait", None)

    def release(self):
        """Снять свою запись: попытка оказалась не неудачной (вход удался или
        это был не вход, а шаг «введите пароль»)."""
        if self.stamp is None:
            return
        history = [t for t in cache.get(self.key, []) if t != self.stamp]
        if history:
            cache.set(self.key, history, self.duration)
        else:
            cache.delete(self.key)
        self.stamp = None

    def reset(self):
        cache.delete(self.key)
        self.stamp = None


class _FailureThrottle(SimpleRateThrottle):
    """Основа для лимитов входа: см. описание модуля."""

    def __init__(self):
        self.rate = api_settings.DEFAULT_THROTTLE_RATES[self.scope]
        super().__init__()
        self.attempts = None

    def get_ident_key(self, request):
        raise NotImplementedError

    def allow_request(self, request, view):
        ident = self.get_ident_key(request)
        if ident is None:
            return True  # не за что зацепиться
        self.attempts = Attempts(self.scope, ident)
        if not self.attempts.take():
            self._wait = self.attempts.wait()
            return False
        # Чтобы вьюха после проверки пароля могла снять запись.
        request._login_attempts = getattr(request, "_login_attempts", []) + [self]
        return True

    def wait(self):
        return getattr(self, "_wait", None)

    reset_on_success = False


def login_succeeded(request):
    """Вход удался — не считаем эту попытку; счётчик аккаунта обнуляем."""
    for throttle in getattr(request, "_login_attempts", []):
        if throttle.attempts is None:
            continue
        if throttle.reset_on_success:
            throttle.attempts.reset()
        else:
            throttle.attempts.release()


def login_not_an_attempt(request):
    """Запрос не был попыткой войти (например, клиент только ввёл телефон)."""
    for throttle in getattr(request, "_login_attempts", []):
        if throttle.attempts is not None:
            throttle.attempts.release()


class LoginIpThrottle(_FailureThrottle):
    """Неудачные попытки входа с одного адреса — и сотрудника, и клиента."""

    scope = "login"

    def get_ident_key(self, request):
        return client_ip(request.META)


class LoginAccountThrottle(_FailureThrottle):
    """Неудачные попытки входа в ОДИН аккаунт, с каких бы адресов они ни шли."""

    scope = "login-account"
    reset_on_success = True

    def get_ident_key(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        return account_key(data.get("username"), data.get("phone"))


class CustomerLoginThrottle(LoginIpThrottle):
    """Кабинет клиента: свой счётчик, чтобы перебор паролей клиентов не закрывал
    вход сотрудникам (и наоборот)."""

    scope = "customer-login"
