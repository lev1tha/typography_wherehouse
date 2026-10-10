"""Фильтры списков «Финансов»: кассовая книга (cash-05, G1-N1)."""
import django_filters
from django.db.models import Q

from .models import CashEntry


class CashEntryFilter(django_filters.FilterSet):
    """Поиск по кассе так, как его делают в Excel автофильтром: по сумме
    (точно или вилкой), кассиру, номеру заказа, клиенту, примечанию, отметке
    «сверено с выпиской». Границы дат — `date_from`/`date_to` во вьюхе."""

    amount = django_filters.NumberFilter(field_name="amount")
    amount_min = django_filters.NumberFilter(field_name="amount", lookup_expr="gte")
    amount_max = django_filters.NumberFilter(field_name="amount", lookup_expr="lte")
    order = django_filters.NumberFilter(field_name="receipt__order_number")
    reconciled = django_filters.BooleanFilter(field_name="reconciled")
    search = django_filters.CharFilter(method="filter_search")

    class Meta:
        model = CashEntry
        fields = ["account", "kind", "article", "created_by", "receipt", "reconciled"]

    def filter_search(self, queryset, name, value):
        value = (value or "").strip()
        if not value:
            return queryset
        q = (
            Q(note__icontains=value)
            | Q(receipt__client__full_name__icontains=value)
            | Q(receipt__client__company_name__icontains=value)
            | Q(receipt__client__phone__icontains=value)
            | Q(created_by__username__icontains=value)
            | Q(expense__name__icontains=value)
        )
        digits = value.lstrip("№#").strip()
        if digits.isdigit() and len(digits) < 10:
            q |= Q(receipt__order_number=int(digits))
        return queryset.filter(q)
