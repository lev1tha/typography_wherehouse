from decimal import Decimal

from django.db.models import Case, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdmin, IsNotAccountant
from audit.models import AuditLog
from clients.models import Client
from clients.phones import find_client_by_phone
from clients.serializers import ClientSerializer
from integrations.telegram import notify_customer, send_customer_receipt
from warehouse.models import InventoryLog
from warehouse.rolls import InsufficientStock

from .models import Receipt, TransactionItem
from .sale_service import (
    ItemEditRejected,
    OrderClosed,
    PaymentRejected,
    add_items_to_receipt,
    apply_payment,
    create_sale,
    day_to_moment,
    delete_receipt,
    give_change,
    parse_amount,
    parse_paid_on,
    receipt_summary,
    refund_receipt,
    update_receipt_items,
)
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


def _parse_date(value):
    """'YYYY-MM-DD' → date, иначе None (пустой/битый ввод = без фильтра)."""
    from datetime import date as _date

    try:
        return _date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class ReceiptViewSet(viewsets.ModelViewSet):
    """Sales / receipts. Storekeepers create sales and issue refunds; admins
    see everything with filtering by date, cashier, payment method and status.

    Фильтры списка: `?client=` (карточка клиента), `?date_from=&date_to=` (дата
    ЗАКАЗА, не оплаты), плюс способ оплаты и статус. Клиента и период добавили
    потому, что «найти все заказы Тахира за июль» через поиск по строке не
    делается: поиск ищет одно слово, а не пересечение двух условий.
    """

    queryset = Receipt.objects.select_related("client", "cashier").prefetch_related(
        "items__material", "items__service", "payments"
    )
    serializer_class = ReceiptSerializer
    # Бухгалтер чеки видит (с себестоимостью и маржой), но не оформляет: он
    # проверяющий, а не участник продажи.
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filterset_fields = ["payment_method", "payment_status", "status", "cashier", "client"]
    search_fields = ["order_number", "title", "client__phone", "client__full_name", "client__company_name"]
    # По умолчанию: у кого долг выше — тот вверху, затем по дате (новые выше).
    # Долг — вычисляемое поле, поэтому аннотируем `_debt` в get_queryset.
    ordering = ["-_debt", "-created_at"]
    # Разрешённые колонки для сортировки по клику (?ordering=...).
    ordering_fields = ["_debt", "created_at", "total_price", "change_due"]

    def get_queryset(self):
        qs = super().get_queryset()
        # Период — по ДАТЕ ЗАКАЗА (`created_at`), той же, по которой считаются
        # выручка и складской лист. Заказ задним числом ищется по своей дате, а
        # не по дню, когда его завели.
        d_from = _parse_date(self.request.query_params.get("date_from"))
        d_to = _parse_date(self.request.query_params.get("date_to"))
        if d_from:
            qs = qs.filter(created_at__date__gte=d_from)
        if d_to:
            qs = qs.filter(created_at__date__lte=d_to)
        # ?has_change=1 — только заказы, по которым цех не вернул сдачу. Это
        # рабочий список кассира: «кому мы ещё должны отдать».
        if self.request.query_params.get("has_change") == "1":
            qs = qs.filter(change_due__gt=0)
        # Чеки видит весь персонал, а не только тот, кто их оформил.
        #
        # Раньше складовщику отдавались только его собственные чеки, и в цехе на
        # двух человек это ломало выдачу: заказ принял админ, выдаёт складовщик —
        # а найти заказ он не может, у него пустой экран. Кто оформил, видно в
        # самом чеке отдельной колонкой.
        #
        # Деньги при этом остаются за админом: оплата долга и откат оплаты
        # закрыты отдельной проверкой, финансовые разделы — паролем.
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
        # Сдача — долг цеха ПЕРЕД клиентом, зеркальный обычному долгу, поэтому
        # стоит рядом с ним отдельной плиткой, а не прячется внутри чеков.
        change = active.aggregate(
            v=Coalesce(Sum("change_due"), Decimal("0"), output_field=DecimalField())
        )["v"]
        return Response(
            {
                "total": qs.count(),
                "working": working,
                "ready": ready,
                "debt": debt,
                "change_due": change,
            }
        )

    @action(detail=False, methods=["get"])
    def titles(self, request):
        """Ранее использованные наименования заказов — для подсказки в кассе,
        как живой поиск по клиенту. Свежие сверху, без повторов."""
        seen, out = set(), []
        rows = (
            self.get_queryset()
            .exclude(title="")
            .order_by("-created_at")
            .values_list("title", flat=True)[:200]
        )
        for title in rows:
            key = title.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(title.strip())
            if len(out) >= 30:
                break
        return Response(out)

    def _fresh_response(self, receipt):
        """Re-load the receipt so the response reflects mutations (the loaded
        instance carries a stale prefetch cache after add/refund/status changes)."""
        fresh = self.get_queryset().get(pk=receipt.pk)
        return Response(ReceiptSerializer(fresh, context={"request": self.request}).data)

    # --- Правка и удаление ошибочного чека -------------------------------
    #
    # Раньше `PATCH`/`DELETE` были открыты всем авторизованным и без единой
    # проверки: складовщик мог переписать `total_price` любым числом, а DELETE
    # стирал чек, НЕ возвращая материал на склад. Кнопок в интерфейсе не было,
    # поэтому дыру никто не замечал — «изменить чек нельзя» и «чек защищён» это
    # разные вещи, и второго не было.
    #
    # Теперь: править можно только то, что не двигает деньги и склад, удалять —
    # целиком и с возвратом материала. И то и другое — админ.
    EDITABLE_FIELDS = {"title", "client", "order_date"}

    def update(self, request, *args, **kwargs):
        return self._edit_meta(request, partial=kwargs.get("partial", False))

    def partial_update(self, request, *args, **kwargs):
        return self._edit_meta(request, partial=True)

    def _edit_meta(self, request, *, partial):
        """PATCH /receipts/<id>/ — правка наименования, клиента и ДАТЫ ЗАКАЗА.

        Состав чека тут не меняется: пересчёт позиций тянет за собой списание со
        склада, себестоимость по партиям FIFO и уже принятые оплаты. Ошибочный
        состав исправляется удалением и повторным вводом — так ни одна из этих
        цифр не разъедется.
        """
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Править чеки может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        unknown = set(request.data) - self.EDITABLE_FIELDS
        if unknown:
            return Response(
                {
                    "detail": (
                        "Здесь можно поменять только наименование, клиента и дату "
                        "заказа. Ошибочный состав — удалить чек и завести заново. "
                        f"Отклонено: {', '.join(sorted(unknown))}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        receipt = self.get_object()
        if "title" in request.data:
            receipt.title = (request.data.get("title") or "").strip()[:255]
        if "client" in request.data:
            raw = request.data.get("client")
            if raw in (None, "", 0):
                receipt.client = None
            elif Client.objects.filter(pk=raw).exists():
                receipt.client_id = raw
            else:
                return Response(
                    {"detail": "Клиент не найден."}, status=status.HTTP_400_BAD_REQUEST
                )
        moment = None
        if "order_date" in request.data:
            day = _parse_date(request.data.get("order_date"))
            if day is None:
                return Response(
                    {"detail": "Некорректная дата заказа."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if day > timezone.localdate():
                return Response(
                    {"detail": "Дата заказа не может быть в будущем."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            moment = day_to_moment(day)
            receipt.created_at = moment
        receipt.save(update_fields=["title", "client", "created_at", "updated_at"])
        if moment is not None:
            # Списание материала передвигаем следом: дата заказа опорная для
            # ВСЕЙ отчётности, и расход, оставшийся в прежнем месяце, увёл бы
            # складской лист от выручки.
            receipt.inventory_logs.filter(type=InventoryLog.Type.SALE).update(
                happened_at=moment
            )

        AuditLog.record(request.user, f"Правка чека {receipt.order_number}: {', '.join(sorted(request.data))}")
        return self._fresh_response(receipt)

    def destroy(self, request, *args, **kwargs):
        """DELETE /receipts/<id>/ — удалить ошибочно заведённый чек целиком.

        Материал возвращается на склад, оплаты снимаются, движения по чеку
        уходят из журнала склада. В журнале ДЕЙСТВИЙ остаётся запись с составом
        удалённого чека — иначе на вопрос «а что там было» ответить нечем.
        """
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Удалять чеки может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        receipt = self.get_object()
        summary = receipt_summary(receipt)
        delete_receipt(receipt, user=request.user)
        AuditLog.record(request.user, f"Удалён чек {summary}")
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _resolve_inline_client(self, client_data):
        """Resolve the inline `client` payload to a Client.

        The frontend may submit a client dict even when the phone already
        belongs to an existing client (the cashier typed the phone without
        picking the live-search match). In that case we reuse the existing
        record instead of failing on the unique-phone validator, and we fill in
        the referrer if it was not set yet (a referral is locked once set, and a
        client can never refer themselves).

        Совпадение ищем ПО ЦИФРАМ номера, а не по строке: раньше сравнивалось
        точное написание, и `0555 111 222` заводил второго клиента поверх
        `+996555111222`. Один человек превращался в двух, а его заказы и долг
        расходились по двум карточкам.
        """
        phone = (client_data.get("phone") or "").strip()
        referred_by_id = client_data.get("referred_by")
        existing = find_client_by_phone(phone) if phone else None
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

        # Заказ задним числом оформляет только админ: дата заказа — опорная для
        # выручки, прибыли по дням и складского листа, то есть правит деньги уже
        # закрытых месяцев. Складовщик оформляет продажу сегодняшним днём.
        order_date = data.get("order_date")
        if order_date and order_date != timezone.localdate():
            if not request.user.is_admin_role:
                return Response(
                    {"detail": "Оформить заказ задним числом может только администратор."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            receipt = create_sale(
                client=client,
                cashier=request.user,
                payment_method=data["payment_method"],
                items_data=data["items"],
                amount_paid=data.get("amount_paid"),
                title=data.get("title", ""),
                created_at=day_to_moment(order_date),
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

    # Деньги и откат оплаты — только админ: складовщик оформляет продажу,
    # но не решает, погашен ли долг и не отменяет принятую оплату.
    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def pay(self, request, pk=None):
        """POST /receipts/<id>/pay/ — принять оплату долга (полную или частичную).

        Увеличивает amount_paid; когда долг погашен — статус становится PAID.
        Принесли больше долга — лишнее записывается СДАЧЕЙ (`keep_change`), а не
        отбрасывается: деньги в кассе, и цех должен их клиенту.
        Необязательные `paid_on` (дата задним числом) и `method` попадают в
        запись оплаты; общая выплата за несколько заказов — в разделе клиентов
        (POST /clients/<id>/pay-debt/).
        """
        receipt = self.get_object()
        try:
            amount = apply_payment(
                receipt,
                parse_amount(request.data.get("amount")),
                user=request.user,
                paid_on=parse_paid_on(request.data.get("paid_on")),
                method=request.data.get("method") or None,
                keep_change=True,
            )
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

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

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
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
        # Сдача уходит вместе с оплатой: откат означает «денег не брали», а
        # сдача — это часть тех же денег. Оставить её значило бы, что цех должен
        # клиенту сдачу с платежа, которого не было.
        receipt.change_due = Decimal("0")
        receipt.payment_status = Receipt.PaymentStatus.PENDING
        receipt.save(
            update_fields=["amount_paid", "change_due", "payment_status", "updated_at"]
        )
        # Записи оплат тоже убираем: откат означает «денег не брали», а
        # оставшаяся запись показывала бы в истории клиента платёж, которого нет.
        receipt.payments.all().delete()

        AuditLog.record(request.user, f"Откат оплаты по чеку {receipt.order_number}: −{returned} сом")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], url_path="edit-items", permission_classes=[IsAdmin])
    def edit_items(self, request, pk=None):
        """POST /receipts/<id>/edit-items/ — править состав чека.

        Тело: `items` — список `{id, quantity?, price_per_item?, remove?}`.
        Склад, себестоимость, итог, статус оплаты и сдача пересчитываются;
        если итог упал ниже уже принятых денег, разница становится сдачей.
        """
        receipt = self.get_object()
        changes = request.data.get("items")
        if not isinstance(changes, list) or not changes:
            return Response(
                {"detail": "Не передано ни одной правки."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        before = receipt_summary(receipt)
        try:
            update_receipt_items(receipt, changes, user=request.user)
        except ItemEditRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientStock as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        receipt.refresh_from_db()
        AuditLog.record(
            request.user,
            f"Правка состава чека {receipt.order_number}: было {before} → стало "
            f"{receipt_summary(receipt)}",
        )
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], url_path="give-change", permission_classes=[IsAdmin])
    def give_change_action(self, request, pk=None):
        """POST /receipts/<id>/give-change/ — отдать клиенту сдачу.

        Пустая сумма — отдать всю. Частично можно: мелочи в кассе может не
        хватить и во второй раз, «отдал тысячу из полутора» — рабочая ситуация.
        """
        receipt = self.get_object()
        try:
            given = give_change(receipt, parse_amount(request.data.get("amount")), user=request.user)
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        AuditLog.record(
            request.user,
            f"Выдана сдача по чеку {receipt.order_number}: −{given} сом "
            f"(осталось {receipt.change_due})",
        )
        if receipt.client and receipt.change_due <= 0:
            notify_customer(
                receipt.client,
                f"💵 Вам выдана сдача {given} сом по заказу №{receipt.order_number}.",
            )
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
