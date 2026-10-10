from rest_framework import serializers

from .kinds import classify
from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    # Тип записи для значка и фильтра: сохранённый, а у старых записей — по тексту.
    kind = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "user", "username", "kind", "action", "created_at"]

    def get_kind(self, obj) -> str:
        return obj.kind or classify(obj.action)
