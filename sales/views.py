from decimal import Decimal, InvalidOperation

from django.db.models import Case, DecimalField, F, Q, Value, When
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from audit.models import AuditLog
from clients.models import Client
from clients.serializers import ClientSerializer
from integrations.telegram import notify_customer, send_customer_receipt
from warehouse.rolls import InsufficientStock

from .models import Receipt, TransactionItem
from .sale_service import OrderClosed, add_items_to_receipt, create_sale, refund_receipt
from .serializers import (
    RefundSerializer,
    ReceiptSerializer,
    SaleCreateSerializer,
    SaleItemInputSerializer,
)


def _receipt_lines_text(receipt: Receipt) -> str:
    lines = []
    for item in receipt.items.all():
        target = item.material.name if item.material_id else item.service.name
        lines.append(f"• {target} × {item.quantity} = {item.line_total} сом")
    return "\n".join(lines)


class ReceiptViewSet(viewsets.ModelViewSet):
    """Sales / receipts. Storekeepers create sales and issue refunds; admins
    see everything with filtering by date, cashier, payment method and status.
    """

    queryset = Receipt.objects.select_related("client", "cashier").prefetch_related(
        "items__material", "items__service"
    )
    serializer_class = ReceiptSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["payment_method", "payment_status", "status", "cashier", "client"]
    search_fields = ["order_number", "client__phone", "client__full_name", "client__company_name"]
    # По умолчанию: у кого долг выше — тот вверху, затем по дате (новые выше).
    # Долг — вычисляемое поле, поэтому аннотируем `_debt` в get_queryset.
    ordering = ["-_debt", "-created_at"]
    # Разрешённые колонки для сортировки по клику (?ordering=...).
    ordering_fields = ["_debt", "created_at", "total_price"]

    def get_queryset(self):
        qs = super().get_queryset()
        # Storekeepers see only their own receipts; admins see all.
        if not self.request.user.is_admin_role:
            qs = qs.filter(cashier=self.request.user)
        # Долг = остаток (сумма − оплачено − возвраты) для открытых чеков, иначе 0.
        # Совпадает с логикой свойства Receipt.debt; используется для сортировки.
        return qs.annotate(
            _debt=Case(
                When(
                    Q(payment_status=Receipt.PaymentStatus.PENDING)
                    & ~Q(status=Receipt.Status.CANCELLED)
                    & Q(total_price__gt=F("amount_paid") + F("refunded_amount")),
                    then=F("total_price") - F("amount_paid") - F("refunded_amount"),
                ),
                default=Value(Decimal("0")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )
        )

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Сводка над списком чеков: всего / в работе / готово / долг. Считается
        по ВСЕМ чекам (с учётом роли и фильтров поиска), а не по одной странице."""
        qs = self.filter_queryset(self.get_queryset())
        active = qs.exclude(status=Receipt.Status.CANCELLED)
        working = (
            active.filter(
                fulfillment_status=Receipt.FulfillmentStatus.PROCESSING,
                items__type=TransactionItem.Type.SERVICE,
            )
            .distinct()
            .count()
        )
        ready = (
            active.filter(
                fulfillment_status=Receipt.FulfillmentStatus.READY,
                items__type=TransactionItem.Type.SERVICE,
            )
            .distinct()
            .count()
        )
        debt = Decimal("0")
        pending = active.filter(payment_status=Receipt.PaymentStatus.PENDING).values_list(
            "total_price", "amount_paid", "refunded_amount"
        )
        for total, paid, refunded in pending:
            owed = total - paid - refunded
            if owed > 0:
                debt += owed
        return Response({"total": qs.count(), "working": working, "ready": ready, "debt": debt})

    def _fresh_response(self, receipt):
        """Re-load the receipt so the response reflects mutations (the loaded
        instance carries a stale prefetch cache after add/refund/status changes)."""
        fresh = self.get_queryset().get(pk=receipt.pk)
        return Response(ReceiptSerializer(fresh, context={"request": self.request}).data)

    def _resolve_inline_client(self, client_data):
        """Resolve the inline `client` payload to a Client.

        The frontend may submit a client dict even when the phone already
        belongs to an existing client (the cashier typed the phone without
        picking the live-search match). In that case we reuse the existing
        record instead of failing on the unique-phone validator, and we fill in
        the referrer if it was not set yet (a referral is locked once set, and a
        client can never refer themselves).
        """
        phone = (client_data.get("phone") or "").strip()
        referred_by_id = client_data.get("referred_by")
        existing = Client.objects.filter(phone=phone).first() if phone else None
        if existing:
            if (
                referred_by_id
                and existing.referred_by_id is None
                and int(referred_by_id) != existing.id
                and Client.objects.filter(pk=referred_by_id).exists()
            ):
                existing.referred_by_id = referred_by_id
                existing.save(update_fields=["referred_by"])
            return existing

        client_serializer = ClientSerializer(data=client_data)
        client_serializer.is_valid(raise_exception=True)
        return client_serializer.save()

    @action(detail=False, methods=["post"], url_path="checkout")
    def checkout(self, request):
        """POST /receipts/checkout/ — create a sale (the main selling flow)."""
        serializer = SaleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        client = data.get("client_id")
        if client is None and data.get("client"):
            client = self._resolve_inline_client(data["client"])

        try:
            receipt = create_sale(
                client=client,
                cashier=request.user,
                payment_method=data["payment_method"],
                items_data=data["items"],
                amount_paid=data.get("amount_paid"),
            )
        except InsufficientStock as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Send the electronic receipt to the customer's Telegram (if linked).
        if client:
            send_customer_receipt(client, receipt, _receipt_lines_text(receipt))

        AuditLog.record(request.user, f"Оформлен чек {receipt.order_number} на {receipt.total_price} сом")
        return Response(
            ReceiptSerializer(receipt, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        """POST /receipts/<id>/refund/ — refund whole receipt or given items."""
        receipt = self.get_object()
        serializer = RefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = refund_receipt(
            receipt,
            item_ids=serializer.validated_data.get("item_ids") or None,
            user=request.user,
        )
        if receipt.client:
            notify_customer(
                receipt.client,
                f"↩️ Оформлен возврат по чеку №{receipt.order_number}. "
                f"Сумма возврата: {receipt.refunded_amount} сом.",
            )
        AuditLog.record(request.user, f"Возврат по чеку {receipt.order_number}")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        """POST /receipts/<id>/pay/ — принять оплату долга (полную или частичную).
        Увеличивает amount_paid; когда долг погашен — статус становится PAID."""
        receipt = self.get_object()
        if receipt.status == Receipt.Status.CANCELLED:
            return Response({"detail": "Чек отменён."}, status=status.HTTP_400_BAD_REQUEST)
        if receipt.payment_status not in (
            Receipt.PaymentStatus.PENDING,
            Receipt.PaymentStatus.PARTIALLY_REFUNDED,
        ):
            return Response({"detail": "По этому чеку долга нет."}, status=status.HTTP_400_BAD_REQUEST)
        target = receipt.total_price - receipt.refunded_amount
        owed = target - receipt.amount_paid
        if owed <= 0:
            return Response({"detail": "По этому чеку долга нет."}, status=status.HTTP_400_BAD_REQUEST)

        raw = request.data.get("amount")
        if raw in (None, ""):
            amount = owed  # по умолчанию — весь долг
        else:
            try:
                amount = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                return Response({"detail": "Некорректная сумма."}, status=status.HTTP_400_BAD_REQUEST)
            # NaN / ±Infinity parse into a Decimal without raising, but NaN then
            # crashes the `<= 0` comparison (HTTP 500) and Infinity silently
            # settles the whole debt — reject any non-finite amount.
            if not amount.is_finite():
                return Response({"detail": "Некорректная сумма."}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"detail": "Сумма должна быть больше 0."}, status=status.HTTP_400_BAD_REQUEST)

        amount = min(amount, owed)  # не больше остатка долга
        receipt.amount_paid = receipt.amount_paid + amount
        if receipt.amount_paid >= target:
            receipt.payment_status = Receipt.PaymentStatus.PAID
        receipt.save(update_fields=["amount_paid", "payment_status", "updated_at"])

        if receipt.client:
            tail = (
                "Долг полностью погашен. Спасибо!"
                if receipt.payment_status == Receipt.PaymentStatus.PAID
                else f"Остаток долга: {receipt.debt} сом."
            )
            notify_customer(
                receipt.client,
                f"💰 Принята оплата {amount} сом по чеку №{receipt.order_number}. {tail}",
            )
        AuditLog.record(request.user, f"Оплата долга по чеку {receipt.order_number}: +{amount} сом")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"])
    def unpay(self, request, pk=None):
        """POST /receipts/<id>/unpay/ — откат оплаты: вернуть чек в «Не оплачено».

        Нужно, если оплату приняли по ошибке (например, не по тому чеку). Обнуляет
        принятую оплату и возвращает весь долг; товар/склад не трогаем (он был
        отгружён при продаже). Недоступно для отменённых и возвращённых чеков.
        """
        receipt = self.get_object()
        if receipt.status == Receipt.Status.CANCELLED:
            return Response({"detail": "Чек отменён."}, status=status.HTTP_400_BAD_REQUEST)
        if receipt.payment_status in (
            Receipt.PaymentStatus.REFUNDED,
            Receipt.PaymentStatus.PARTIALLY_REFUNDED,
        ):
            return Response(
                {"detail": "По возвращённому чеку откат оплаты недоступен."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Откатывать можно оплаченный чек (в т.ч. старый, где amount_paid=0 —
        # поле появилось позже) или чек с частичной предоплатой.
        if receipt.payment_status != Receipt.PaymentStatus.PAID and receipt.amount_paid <= 0:
            return Response(
                {"detail": "По этому чеку нет принятой оплаты."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        returned = receipt.amount_paid
        receipt.amount_paid = Decimal("0")
        receipt.payment_status = Receipt.PaymentStatus.PENDING
        receipt.save(update_fields=["amount_paid", "payment_status", "updated_at"])

        AuditLog.record(request.user, f"Откат оплаты по чеку {receipt.order_number}: −{returned} сом")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], url_path="add-items")
    def add_items(self, request, pk=None):
        """POST /receipts/<id>/add-items/ — дозаказ: add lines to an open order."""
        receipt = self.get_object()
        serializer = SaleItemInputSerializer(data=request.data.get("items", []), many=True)
        serializer.is_valid(raise_exception=True)
        try:
            receipt, surcharge = add_items_to_receipt(
                receipt, serializer.validated_data, user=request.user
            )
        except (OrderClosed, InsufficientStock) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if receipt.client and surcharge:
            notify_customer(
                receipt.client,
                f"➕ В ваш заказ №{receipt.order_number} добавлены позиции на {surcharge} сом. "
                f"Новый итог: {receipt.total_price} сом.",
            )
        AuditLog.record(request.user, f"Дозаказ по чеку {receipt.order_number}: +{surcharge} сом")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], url_path="mark-ready")
    def mark_ready(self, request, pk=None):
        """POST /receipts/<id>/mark-ready/ — service ready, notify customer."""
        receipt = self.get_object()
        receipt.fulfillment_status = Receipt.FulfillmentStatus.READY
        receipt.save(update_fields=["fulfillment_status", "updated_at"])
        if receipt.client:
            notify_customer(
                receipt.client,
                "✅ Ваш заказ по резке букв успешно выполнен и ждёт вас на складе!",
            )
        AuditLog.record(request.user, f"Заказ по чеку {receipt.order_number} готов к выдаче")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], url_path="mark-issued")
    def mark_issued(self, request, pk=None):
        """POST /receipts/<id>/mark-issued/ — order handed to the customer."""
        receipt = self.get_object()
        receipt.fulfillment_status = Receipt.FulfillmentStatus.ISSUED
        receipt.save(update_fields=["fulfillment_status", "updated_at"])
        if receipt.client:
            notify_customer(
                receipt.client, "📦 Ваш заказ выдан. Спасибо, что выбрали нас!"
            )
        AuditLog.record(request.user, f"Заказ по чеку {receipt.order_number} выдан клиенту")
        return self._fresh_response(receipt)
