from rest_framework import generics, permissions
from rest_framework.exceptions import Throttled
from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import CloudeTokenObtainPairSerializer, UserSerializer
from .throttling import LoginAccountThrottle, LoginIpThrottle


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
        raise throttled_response(wait)


class MeView(generics.RetrieveAPIView):
    """GET /api/me/ — the currently authenticated staff user."""

    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user
