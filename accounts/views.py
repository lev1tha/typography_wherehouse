import logging

from django.db import connection
from django.db.models import ProtectedError
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from audit.models import AuditLog

from .authentication import CloudeTokenRefreshSerializer
from .models import Employee, User
from .permissions import IsAdmin, IsAdminOrAccountantRead
from .serializers import (
    CloudeTokenObtainPairSerializer,
    EmployeeSerializer,
    StaffPasswordSerializer,
    StaffUserSerializer,
    UserSerializer,
)
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


class StaffUserViewSet(viewsets.ModelViewSet):
    """Учётные записи сотрудников: создать, сменить роль и пароль, отключить.

    Только администратор. Удалять нельзя — отключают: чеки, журнал и кассовая
    книга хранят автора. Нельзя отключить или понизить самого себя и оставить
    систему без единого действующего администратора.
    """

    serializer_class = StaffUserSerializer
    permission_classes = [IsAdmin]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_fields = ["role", "is_active"]
    search_fields = ["username", "first_name", "last_name"]

    def get_queryset(self):
        return User.objects.select_related("employee").order_by("-is_active", "username")

    def _active_admins_without(self, user):
        return User.objects.filter(role=User.Role.ADMIN, is_active=True).exclude(pk=user.pk).count()

    def perform_create(self, serializer):
        user = serializer.save()
        AuditLog.record(
            self.request.user, f"Создана учётная запись «{user.username}» ({user.get_role_display()})",
            kind="staff",
        )

    def perform_update(self, serializer):
        user = serializer.instance
        data = serializer.validated_data
        becomes_inactive = "is_active" in data and not data["is_active"] and user.is_active
        loses_admin = "role" in data and data["role"] != User.Role.ADMIN and user.role == User.Role.ADMIN
        if (becomes_inactive or loses_admin) and user.pk == self.request.user.pk:
            from rest_framework.exceptions import ValidationError

            raise ValidationError("Нельзя отключить или понизить самого себя.")
        if (becomes_inactive or loses_admin) and user.role == User.Role.ADMIN and user.is_active:
            if self._active_admins_without(user) == 0:
                from rest_framework.exceptions import ValidationError

                raise ValidationError("Это последний действующий администратор — сначала назначьте другого.")
        before = (user.role, user.is_active, user.first_name, user.last_name, user.username)
        updated = serializer.save()
        after = (updated.role, updated.is_active, updated.first_name, updated.last_name, updated.username)
        notes = []
        if before[0] != after[0]:
            notes.append(f"роль {User.Role(before[0]).label} → {User.Role(after[0]).label}")
        if before[1] != after[1]:
            notes.append("отключена" if not after[1] else "включена")
        if before[2:] != after[2:]:
            notes.append(f"имя/логин {before[4]} {before[2]} {before[3]} → {after[4]} {after[2]} {after[3]}".strip())
        if notes:
            AuditLog.record(
                self.request.user, f"Учётная запись «{updated.username}»: " + "; ".join(notes), kind="staff",
            )

    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        """Новый пароль. Старые токены перестают работать (версия учётных данных)."""
        user = self.get_object()
        serializer = StaffPasswordSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
        user.set_password(serializer.validated_data["password"])
        user.save()
        AuditLog.record(request.user, f"Сменён пароль учётной записи «{user.username}»", kind="staff")
        return Response({"ok": True})


class EmployeeViewSet(viewsets.ModelViewSet):
    """Справочник сотрудников (для зарплаты). Админ ведёт, бухгалтер смотрит."""

    serializer_class = EmployeeSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None
    filterset_fields = ["is_active", "default_machine"]
    search_fields = ["full_name", "position"]

    def get_queryset(self):
        return Employee.objects.select_related("user")

    @action(detail=False, methods=["get"], url_path="executors",
            permission_classes=[permissions.IsAuthenticated])
    def executors(self, request):
        """GET /staff/employees/executors/ — кого можно выбрать исполнителем работы
        в кассе (волна 2, STAFF-02). Видит весь персонал: заказ оформляет и
        складовщик. Только работающие, без учёток и примечаний; `me` — сотрудник,
        привязанный к учётке спрашивающего (касса ставит его по умолчанию)."""
        active = Employee.objects.filter(is_active=True).order_by("full_name", "id")
        me = next((e.id for e in active if e.user_id and e.user_id == request.user.id), None)
        return Response({
            "me": me,
            "employees": [
                {"id": e.id, "full_name": e.full_name, "default_machine": e.default_machine}
                for e in active
            ],
        })

    def perform_create(self, serializer):
        e = serializer.save()
        AuditLog.record(self.request.user, f"Сотрудник добавлен: {e.full_name}", kind="staff")

    def perform_update(self, serializer):
        e = serializer.save()
        AuditLog.record(self.request.user, f"Сотрудник изменён: {e.full_name}", kind="staff")

    def destroy(self, request, *args, **kwargs):
        employee = self.get_object()
        try:
            name = employee.full_name
            employee.delete()
        except ProtectedError:
            return Response(
                {"detail": "По сотруднику есть начисления, выплаты или работы в заказах — удалить нельзя, отключите его."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        AuditLog.record(request.user, f"Сотрудник удалён: {name}", kind="staff")
        return Response(status=status.HTTP_204_NO_CONTENT)
