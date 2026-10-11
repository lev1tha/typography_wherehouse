import django_filters

from .kinds import kind_q
from .models import AuditLog

TRUE = ("1", "true", "yes", "on")


class AuditLogFilter(django_filters.FilterSet):
    """Журнал: даты, пользователь, тип и поиск по тексту.

    Входы в систему — половина журнала и шум для владельца (RU-N22, D-189):
    по умолчанию скрыты; `?logins=1` или фильтр «Тип: Вход» их показывает."""

    date_from = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")
    kind = django_filters.CharFilter(method="filter_kind")
    search = django_filters.CharFilter(field_name="action", lookup_expr="icontains")
    # Пользователь по логину (бухгалтер список учёток не читает — id ему взять негде).
    username = django_filters.CharFilter(field_name="user__username", lookup_expr="icontains")
    # Показать входы в систему (по умолчанию скрыты) — разбирается в `filter_queryset`.
    logins = django_filters.CharFilter(method="keep")

    class Meta:
        model = AuditLog
        fields = ["user"]

    def filter_kind(self, queryset, name, value):
        return queryset.filter(kind_q(value)) if value else queryset

    def keep(self, queryset, name, value):
        return queryset

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        data = self.data or {}
        if not data.get("kind") and str(data.get("logins") or "").lower() not in TRUE:
            queryset = queryset.exclude(kind_q("login"))
        return queryset
