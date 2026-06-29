from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from audit.models import AuditLog

from .models import User


class CloudeTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login with username + password. Returns the JWT pair plus role info so
    the frontend can redirect to the Admin or Storekeeper route immediately.
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["username"] = user.username
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
        AuditLog.record(self.user, "Вход в систему")
        return data


class UserSerializer(serializers.ModelSerializer):
    is_admin = serializers.BooleanField(source="is_admin_role", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "role", "is_admin"]
