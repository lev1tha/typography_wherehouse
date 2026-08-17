"""Пределы попыток входа.

Единственные незакрытые токеном двери системы — два логина: сотрудника и
клиентского кабинета. Ограничений на них не было вовсе, а пароль кабинета
короткий (его выдаёт админ), и портал открыт на публичном домене — то есть
подобрать его можно было простым перебором, без единой помехи.

Считаем в двух разрезах:
  * по АДРЕСУ — обычный перебор с одной машины;
  * по ЛОГИНУ (телефону) — распределённый перебор одного и того же аккаунта,
    когда адреса каждый раз новые.

Предел по логину мягче и с часовым окном: он должен мешать подбору, но не
запирать человека, который пару раз ошибся паролем.
"""
from rest_framework.throttling import SimpleRateThrottle


class LoginIpThrottle(SimpleRateThrottle):
    """Попытки входа с одного адреса — и сотрудника, и клиента."""

    scope = "login"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class LoginAccountThrottle(SimpleRateThrottle):
    """Попытки входа в ОДИН аккаунт, с каких бы адресов они ни шли."""

    scope = "login-account"

    def get_cache_key(self, request, view):
        data = request.data if isinstance(request.data, dict) else {}
        account = (data.get("username") or data.get("phone") or "").strip().lower()
        if not account:
            return None  # не за что зацепиться — считает только адресный предел
        return self.cache_format % {"scope": self.scope, "ident": account}


class CustomerLoginThrottle(LoginIpThrottle):
    """Кабинет клиента: свой счётчик, чтобы перебор паролей клиентов не закрывал
    вход сотрудникам (и наоборот)."""

    scope = "customer-login"
