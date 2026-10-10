import django_filters

from .kinds import kind_q
from .models import AuditLog


class AuditLogFilter(django_filters.FilterSet):
    """Журнал: даты, пользователь, тип и поиск по тексту."""

    date_from = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")
    kind = django_filters.CharFilter(method="filter_kind")
    search = django_filters.CharFilter(field_name="action", lookup_expr="icontains")
    # Пользователь по логину (бухгалтер список учёток не читает — id ему взять негде).
    username = django_filters.CharFilter(field_name="user__username", lookup_expr="icontains")

    class Meta:
        model = AuditLog
        fields = ["user"]

    def filter_kind(self, queryset, name, value):
        return queryset.filter(kind_q(value)) if value else queryset
