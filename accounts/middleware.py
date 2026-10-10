"""Предел неудачных попыток для формы входа Django-админки.

Форма `/django-admin/login/` — третья дверь без токена, и лимита на ней не было
(DRF-ные пределы действуют только на API). Тот же счётчик, что у остальных
входов: неудачи по адресу (`login`) и по логину (`login-account`); удачный вход
запись снимает. Неудача админки — это ответ 200 (форма перерисована с ошибкой),
удача — редирект 302.
"""
from django.http import HttpResponse
from django.urls import reverse

from .throttling import Attempts, account_key, client_ip


class AdminLoginThrottleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != "POST" or request.path != reverse("admin:login"):
            return self.get_response(request)

        taken = [Attempts("login", client_ip(request.META))]
        key = account_key(request.POST.get("username"))
        if key is not None:
            taken.append(Attempts("login-account", key))

        granted = []
        wait = 0
        for attempts in taken:
            if attempts.take():
                granted.append(attempts)
            else:
                wait = max(wait, attempts.wait() or 0)
        if len(granted) != len(taken):
            for attempts in granted:  # отказанный запрос чужой счётчик не продлевает
                attempts.release()
            minutes = max(1, int(wait // 60 + (1 if wait % 60 else 0)))
            response = HttpResponse(
                f"Слишком много попыток входа. Попробуйте через {minutes} мин.",
                status=429,
                content_type="text/plain; charset=utf-8",
            )
            response["Retry-After"] = str(int(wait) or 1)
            return response

        response = self.get_response(request)
        if response.status_code == 302:  # вошли
            granted[0].release()
            for attempts in granted[1:]:
                attempts.reset()
        elif response.status_code != 200:  # 403 CSRF и прочее — это не попытка
            for attempts in granted:
                attempts.release()
        return response
