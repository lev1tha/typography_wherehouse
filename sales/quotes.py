"""Коммерческие предложения (2026-10-10, CALC-03).

КП — расчёт для клиента, а не продажа: позиции как в корзине кассы, цены по
правилам прайса, срок действия и своя печатная форма без номера чека. Склад,
долг, касса и выручка КП не трогаются; оформить заказ из КП можно позже —
касса грузит его позиции в корзину (`cartFromReceipt`) и оформляет обычным
`checkout` с `quote_id`.

Оформление по `quote_id` (перепроверка 10.10, D-153): КП должно существовать,
не быть отменённым и ещё не оформленным (400/409). КП в срок и того же состава
оформляется по своим ценам, срочности и скидке; просроченное или изменённое —
по сегодняшним, с подтверждением «КП №X: было Y, сейчас Z». Оформленное КП
ссылается на чек и второй раз не оформляется (`sale_service.quote_for_order`,
`apply_quote_prices`, `mark_quote_ordered`).
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from accounts.permissions import IsNotAccountant
from audit.models import AuditLog
from clients.models import Client

from .models import Quote
from .sale_service import contract_prices_for, drop_unchanged_prices, price_cart
from .serializers import SaleItemInputSerializer, TransactionItemSerializer
from .views import _order_pricing, _price_override_forbidden

DEFAULT_VALID_DAYS = 14


class QuoteSerializer(serializers.ModelSerializer):
    client_label = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default=None)
    receipt_number = serializers.IntegerField(source="receipt.order_number", read_only=True, default=None)
    is_expired = serializers.SerializerMethodField()
    # Строки в форме позиций чека — касса грузит их в корзину тем же кодом, что и
    # «Повторить заказ».
    items = serializers.JSONField(source="lines", read_only=True)

    class Meta:
        model = Quote
        fields = [
            "id", "number", "client", "client_name", "client_label", "title", "note",
            "created_by", "created_by_name", "created_at", "valid_until", "is_expired",
            "status", "total_price", "is_urgent", "urgency_percent", "discount_percent",
            "receipt", "receipt_number", "cart", "items",
        ]
        read_only_fields = fields

    def get_client_label(self, obj):
        if obj.client_id:
            return obj.client.display_name
        return obj.client_name

    def get_is_expired(self, obj):
        return bool(obj.valid_until and obj.valid_until < timezone.localdate())


class QuoteCreateSerializer(serializers.Serializer):
    client_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), required=False, allow_null=True
    )
    client_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    note = serializers.CharField(required=False, allow_blank=True)
    valid_days = serializers.IntegerField(min_value=1, max_value=365, required=False)
    is_urgent = serializers.BooleanField(required=False, default=False)
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        required=False, allow_null=True,
    )
    items = SaleItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Добавьте хотя бы одну позицию.")
        # Кусок с полки (D-201) продаётся только чеком: КП его не держит, а через
        # неделю на полке его может уже не быть.
        if any(item.get("leftover") is not None for item in value):
            raise serializers.ValidationError(
                "Остатки с полки в КП не добавляются — продайте их чеком в кассе."
            )
        return value


def _snapshot(items):
    """Строки КП в форме позиций чека — без закупочных цифр (КП видят все)."""
    data = TransactionItemSerializer(items, many=True, context={}).data
    return json.loads(JSONRenderer().render(data))


class QuoteSearchFilter(SearchFilter):
    """«КП 12», «№12», «12» — номер КП; остальное — слова названия и клиента."""

    def filter_queryset(self, request, queryset, view):
        raw = (request.query_params.get(self.search_param) or "").strip()
        digits = raw.upper().replace("КП", "").replace("№", "").replace("#", "").strip()
        if digits.isdigit():
            return queryset.filter(number=int(digits))
        return super().filter_queryset(request, queryset, view)


class QuoteViewSet(viewsets.ReadOnlyModelViewSet):
    """Коммерческие предложения: список, карточка, создание из корзины, отмена."""

    queryset = Quote.objects.select_related("client", "created_by", "receipt")
    serializer_class = QuoteSerializer
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filter_backends = [QuoteSearchFilter, OrderingFilter]
    search_fields = ["title", "client_name", "client__full_name", "client__company_name", "client__phone"]
    ordering = ["-created_at", "-id"]
    ordering_fields = ["created_at", "number", "total_price"]

    def get_queryset(self):
        qs = super().get_queryset()
        state = self.request.query_params.get("status")
        if state in Quote.Status.values:
            qs = qs.filter(status=state)
        return qs

    def create(self, request, *args, **kwargs):
        """POST /quotes/ — посчитать корзину и сохранить КП.

        Цены — по правилам прайса (минимум, срочность, скидка), как при
        оформлении; ручные цены — те же права, что в кассе. Склад не трогается.
        """
        serializer = QuoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        client = data.get("client_id")
        # Ставка, равная действующей (договорной или каталожной), — не ручная
        # цена (перепроверка 10.10, S1 №1): как в кассе.
        contracts = contract_prices_for(client.pk if client is not None else None)
        for entry in data["items"]:
            drop_unchanged_prices(entry, contracts)
        forbidden = _price_override_forbidden(data["items"], request.user)
        if forbidden:
            return Response({"detail": forbidden}, status=status.HTTP_403_FORBIDDEN)
        pricing, refused = _order_pricing(data, client, request.user)
        if refused:
            return Response({"detail": refused[0]}, status=refused[1])
        is_urgent, urgency_percent, discount_percent = pricing

        with transaction.atomic():
            receipt, built = price_cart(
                client=client, items_data=data["items"], is_urgent=is_urgent,
                urgency_percent=urgency_percent, discount_percent=discount_percent,
            )
            lines = _snapshot(built)
            total = receipt.total_price
            transaction.set_rollback(True)  # временный чек КП не оставляет следа

        days = data.get("valid_days") or DEFAULT_VALID_DAYS
        quote = Quote.objects.create(
            client=client,
            client_name=(data.get("client_name") or "").strip()[:255],
            title=(data.get("title") or "").strip()[:255],
            note=data.get("note") or "",
            created_by=request.user,
            valid_until=timezone.localdate() + timedelta(days=days),
            cart=request.data.get("items") or [],
            lines=lines,
            total_price=total,
            is_urgent=is_urgent,
            urgency_percent=urgency_percent,
            discount_percent=discount_percent,
        )
        AuditLog.record(request.user, f"Создано КП №{quote.number} на {quote.total_price} сом")
        return Response(
            QuoteSerializer(quote, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """POST /quotes/<id>/cancel/ — отменить КП (оформленное в заказ — нельзя)."""
        quote = self.get_object()
        if quote.status == Quote.Status.ORDERED:
            return Response(
                {"detail": "Из этого КП уже оформлен заказ — отменять его нельзя."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        quote.status = Quote.Status.CANCELLED
        quote.save(update_fields=["status"])
        AuditLog.record(request.user, f"Отменено КП №{quote.number}")
        return Response(QuoteSerializer(quote, context={"request": request}).data)

    def destroy(self, request, *args, **kwargs):
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Удалять КП может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        quote = self.get_object()
        AuditLog.record(request.user, f"Удалено КП №{quote.number} на {quote.total_price} сом")
        quote.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
