"""JWT с отзывом при смене пароля.

В токен кладётся клейм `cv` — версия учётных данных пользователя
(`User.credentials_version`). Смена пароля версию увеличивает, и токены,
выданные раньше, перестают приниматься — иначе украденный токен жил бы до конца
своего срока (access — 12 ч, refresh — 7 дней) даже после смены пароля.

Токен БЕЗ клейма — выданный до введения версии — считается версией 0: он
валиден, пока пароль не меняли. Массового разлогина при выкладке нет.
"""
from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings

CREDENTIALS_CLAIM = "cv"
CHANGED_MESSAGE = "Пароль изменён — войдите заново."


def token_version(token) -> int:
    """Версия учётных данных, записанная в токене (нет клейма — 0)."""
    try:
        return int(token.get(CREDENTIALS_CLAIM, 0))
    except (TypeError, ValueError):
        return -1  # мусор в клейме не равен ни одной настоящей версии


class CloudeJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if token_version(validated_token) != user.credentials_version:
            raise AuthenticationFailed(CHANGED_MESSAGE, code="credentials_changed")
        return user


class CloudeTokenRefreshSerializer(TokenRefreshSerializer):
    """Обновление токена тоже сверяет версию: старый refresh после смены пароля
    нового access не даёт."""

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])
        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
        user = get_user_model().objects.filter(
            **{api_settings.USER_ID_FIELD: user_id}
        ).first()
        if user is None or not api_settings.USER_AUTHENTICATION_RULE(user):
            raise AuthenticationFailed(
                self.error_messages["no_active_account"], "no_active_account"
            )
        if token_version(refresh) != user.credentials_version:
            raise AuthenticationFailed(CHANGED_MESSAGE, code="credentials_changed")
        return super().validate(attrs)
