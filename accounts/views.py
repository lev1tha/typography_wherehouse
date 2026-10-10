import logging

from django.db import connection
from rest_framework import generics, permissions, status
from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .authentication import CloudeTokenRefreshSerializer
from .serializers import CloudeTokenObtainPairSerializer, UserSerializer
from .throttling import (
    LoginAccountThrottle,
    LoginIpThrottle,
    login_not_an_attempt,
    login_succeeded,
)

logger = logging.getLogger(__name__)


def throttled_response(wait):
    """Понятный отказ вместо «Request was throttled. Expected available in…».

    Человек за кассой должен видеть, что делать: подождать столько-то минут, а
    не английскую строку из библиотеки.
    """
    minutes = max(1, int((wait or 0) // 60 + (1 if (wait or 0) % 60 else 0)))
    # `wait` в конструктор НЕ передаём: DRF пришил бы к нашему тексту свой
    # английский хвост «Expected available in 58 seconds». Само значение
    # проставляем полем — из него собирается заголовок Retry-After.
    exc = Throttled(detail=f"Слишком много попыток входа. Попробуйте через {minutes} мин.")
    exc.wait = wait
    return exc


class CloudeTokenObtainPairView(TokenObtainPairView):
    """POST /api/token/ — login, returns JWT pair + user role."""

    serializer_class = CloudeTokenObtainPairSerializer
    # Единственная дверь без токена — здесь и перебирают пароли.
    throttle_classes = [LoginIpThrottle, LoginAccountThrottle]

    def throttled(self, request, wait):
        # Отказанный запрос не должен продлевать чужой счётчик (адрес человека,
        # который ломится в заблокированный аккаунт, иначе заперся бы сам).
        login_not_an_attempt(request)
        raise throttled_response(wait)

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        # Сюда доходим только при удачном входе: неверный пароль — исключение.
        login_succeeded(request)
        return response


class CloudeTokenRefreshView(TokenRefreshView):
    """POST /api/token/refresh/ {refresh} → {access, refresh}.

    Refresh ротируется: каждый вызов отдаёт новый и гасит прежний (чёрный
    список). Фронт обязан сохранить присланный `refresh`.
    """

    serializer_class = CloudeTokenRefreshSerializer


class MeView(generics.RetrieveAPIView):
    """GET /api/me/ — the currently authenticated staff user."""

    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class HealthView(APIView):
    """GET /api/health/ — для docker healthcheck и мониторинга.

    Без авторизации и без лимита: его дёргает сам контейнер каждые несколько
    секунд. Делает `SELECT 1` и больше ничего; в ответе — только статус, ни
    версий, ни имён базы, ни текста ошибки (она уходит в лог).
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            logger.exception("health: база недоступна")
            return Response(
                {"status": "db_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
                headers={"Cache-Control": "no-store"},
            )
        return Response({"status": "ok"}, headers={"Cache-Control": "no-store"})
