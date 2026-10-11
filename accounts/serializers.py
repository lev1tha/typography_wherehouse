from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from audit.models import AuditLog

from .models import Employee, User


class CloudeTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login with username + password. Returns the JWT pair plus role info so
    the frontend can redirect to the Admin or Storekeeper route immediately.
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["username"] = user.username
        token["cv"] = user.credentials_version
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = {
            "id": self.user.id,
            "username": self.user.username,
            "role": self.user.role,
            "is_admin": self.user.is_admin_role,
            "full_name": self.user.get_full_name(),
        }
        AuditLog.record(self.user, "Вход в систему", kind="login")
        return data


class UserSerializer(serializers.ModelSerializer):
    is_admin = serializers.BooleanField(source="is_admin_role", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "role", "is_admin"]


def _check_password(value, user=None):
    """Пароль по правилам проекта (длина, не только цифры, не из топа частых)."""
    try:
        validate_password(value, user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError(list(exc.messages))
    return value


class StaffUserSerializer(serializers.ModelSerializer):
    """Учётная запись для экрана «Сотрудники» (только админ).

    Пароль принимается при создании и отдельным действием «сменить пароль» —
    назад не отдаётся никогда. Удаления нет: уволенного отключают (`is_active`),
    чтобы журнал действий и чеки сохранили автора.
    """

    password = serializers.CharField(write_only=True, required=False, allow_blank=False, trim_whitespace=False)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    employee = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name", "role", "role_display",
            "is_active", "password", "last_login", "date_joined", "employee",
        ]
        read_only_fields = ["last_login", "date_joined"]

    def get_employee(self, obj):
        employee = getattr(obj, "employee", None)
        return employee.id if employee else None

    def validate_username(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Укажите логин.")
        clash = User.objects.filter(username__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("Такой логин уже есть.")
        return value

    def validate(self, attrs):
        if self.instance is None:
            if not attrs.get("password"):
                raise serializers.ValidationError({"password": "Задайте пароль."})
            try:
                _check_password(attrs["password"], User(username=attrs.get("username", "")))
            except serializers.ValidationError as exc:
                # Ошибка — у поля «пароль», а не общая: форма покажет её под полем.
                raise serializers.ValidationError({"password": exc.detail})
        elif "password" in attrs:
            raise serializers.ValidationError({"password": "Пароль меняется отдельным действием."})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class StaffPasswordSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_password(self, value):
        return _check_password(value, self.context.get("user"))


class EmployeeSerializer(serializers.ModelSerializer):
    """Справочник сотрудников, которым считают зарплату."""

    machine_display = serializers.CharField(source="get_default_machine_display", read_only=True)
    user_username = serializers.CharField(source="user.username", read_only=True, default=None)

    class Meta:
        model = Employee
        fields = [
            "id", "full_name", "position", "default_machine", "machine_display",
            "is_active", "user", "user_username", "note", "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_full_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Укажите ФИО.")
        return value

    def validate_user(self, user):
        if user is None:
            return user
        clash = Employee.objects.filter(user=user)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("Эта учётная запись уже привязана к другому сотруднику.")
        return user
