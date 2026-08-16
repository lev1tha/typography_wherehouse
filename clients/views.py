import secrets
from datetime import date
from decimal import Decimal

from django.db.models import (
    Case,
    Count,
    DecimalField,
    Exists,
    F,
    OuterRef,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower, NullIf
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from finance.periods import ensure_open
from accounts.permissions import IsAdmin, IsNotAccountant
from audit.models import AuditLog

from .customer import MIN_PORTAL_PASSWORD
from .merge import MergeRejected, merge_clients, merge_summary
from .models import Client, ReferralChangeRequest
from .serializers import (
    ClientDetailSerializer,
    ClientSerializer,
    ReferralChangeRequestSerializer,
)


def _parse_date(value):
    """'YYYY-MM-DD' → date, иначе None (пустой/битый ввод = без фильтра)."""
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class ClientViewSet(viewsets.ModelViewSet):
    """CRM. Live ?search= lookup (по имени, телефону или НОМЕРУ ЗАКАЗА) для
    быстрого автозаполнения в кассе. Поиск регистронезависимый на любой БД
    (фильтрация в Python — SQLite не умеет регистронезависимый LIKE для кириллицы).

    Фильтр периода ?date_from=&date_to= оставляет клиентов, у которых были заказы
    в эти дни; «Заказов» тогда считается за тот же период. Сортировка по клику —
    ?ordering=orders_count|-orders_count|debt|-debt|sort_name.
    """

    queryset = Client.objects.all()
    # Бухгалтер карточки клиентов не заводит и не правит — он проверяющий.
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filterset_fields = ["type"]
    # search_fields НЕ задаём: DRF SearchFilter использует icontains, который на
    # SQLite не находит кириллицу в другом регистре. Ищем сами в get_queryset.
    ordering = ["sort_name"]
    ordering_fields = ["sort_name", "orders_count", "debt", "change_due_total", "created_at"]

    def _period(self):
        return (
            _parse_date(self.request.query_params.get("date_from")),
            _parse_date(self.request.query_params.get("date_to")),
        )

    def get_serializer_context(self):
        # Карточка клиента показывает заказы за тот же период, что и список.
        ctx = super().get_serializer_context()
        d_from, d_to = self._period()
        ctx["date_from"], ctx["date_to"] = d_from, d_to
        return ctx

    def get_queryset(self):
        from sales.models import Receipt

        # Сортировка по ИМЕНИ (компания или ФИО), с откатом на телефон — не по дате.
        # На карточке (retrieve) грузим и позиции чеков — для списка заказов клиента.
        prefetch = ["receipts"]
        if self.action == "retrieve":
            prefetch = [
                "receipts__items__material",
                "receipts__items__service",
                "referrals",
            ]

        d_from, d_to = self._period()
        live = ~Q(receipts__status=Receipt.Status.CANCELLED)
        in_period = Q()
        if d_from:
            in_period &= Q(receipts__created_at__date__gte=d_from)
        if d_to:
            in_period &= Q(receipts__created_at__date__lte=d_to)

        # Долг — «на сейчас», период его не двигает: клиент должен независимо от
        # того, за какой месяц мы смотрим заказы. Формула та же, что в
        # Receipt.debt и в сортировке чеков.
        debt_case = Case(
            When(
                Q(receipts__payment_status__in=Receipt.OWING_STATUSES)
                & ~Q(receipts__status=Receipt.Status.CANCELLED)
                & Q(
                    receipts__total_price__gt=F("receipts__amount_paid")
                    + F("receipts__refunded_amount")
                ),
                then=F("receipts__total_price")
                - F("receipts__amount_paid")
                - F("receipts__refunded_amount"),
            ),
            default=Value(Decimal("0")),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )

        qs = (
            Client.objects.annotate(
                sort_name=Lower(
                    Coalesce(NullIf("company_name", Value("")), NullIf("full_name", Value("")), "phone")
                ),
                # distinct — иначе join по позициям чеков посчитал бы заказы по разу
                # на каждую строку чека.
                orders_count=Count("receipts", filter=live & in_period, distinct=True),
                debt=Coalesce(
                    Sum(debt_case),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                # Сдача — зеркало долга: сколько ЦЕХ должен клиенту. Считается
                # так же, «на сейчас», и период её не режет: деньги лежат в
                # кассе независимо от того, какой месяц выбран в фильтре.
                change_due_total=Coalesce(
                    Sum("receipts__change_due", filter=live),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
            )
            .prefetch_related(*prefetch)
            .order_by("sort_name")
        )

        # Период сужает СПИСОК клиентов через отдельный подзапрос, а не через тот
        # же join — иначе он обрезал бы и долг, который должен быть «на сейчас».
        if d_from or d_to:
            recent = Receipt.objects.filter(client=OuterRef("pk")).exclude(
                status=Receipt.Status.CANCELLED
            )
            if d_from:
                recent = recent.filter(created_at__date__gte=d_from)
            if d_to:
                recent = recent.filter(created_at__date__lte=d_to)
            qs = qs.filter(Exists(recent))

        # Фильтр «только должники» — по той же аннотации, что и сортировка.
        if self.request.query_params.get("has_debt") in ("1", "true", "True"):
            qs = qs.filter(debt__gt=0)

        # «Кому мы должны сдачу» — обратный список к должникам.
        if self.request.query_params.get("has_change") in ("1", "true", "True"):
            qs = qs.filter(change_due_total__gt=0)

        # Фильтр «заказов от N». Кривое значение игнорируем, а не роняем список.
        try:
            min_orders = int(self.request.query_params.get("min_orders") or 0)
        except (TypeError, ValueError):
            min_orders = 0
        if min_orders > 0:
            qs = qs.filter(orders_count__gte=min_orders)

        search = (self.request.query_params.get("search") or "").strip().lower()
        if search:
            ids = [
                c.id
                for c in Client.objects.only("id", "full_name", "company_name", "phone")
                if search in (c.full_name or "").lower()
                or search in (c.company_name or "").lower()
                or search in (c.phone or "").lower()
            ]
            # Поиск по номеру чека: «5» находит клиента, у которого заказ №5.
            if search.lstrip("№").isdigit():
                ids += list(
                    Receipt.objects.filter(order_number=int(search.lstrip("№")))
                    .exclude(client__isnull=True)
                    .values_list("client_id", flat=True)
                )
            qs = qs.filter(id__in=set(ids))
        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ClientDetailSerializer
        return ClientSerializer

    @action(detail=True, methods=["post"], url_path="set-password", permission_classes=[IsAdmin])
    def set_password(self, request, pk=None):
        """Выдать клиенту пароль от кабинета. Только администратор.

        Пароль можно передать в `password`, иначе генерируем. Возвращаем его
        ОДИН раз — в базе лежит только хеш, посмотреть повторно нельзя, можно
        лишь выдать новый. Сюда же сводится и «клиент забыл пароль».
        """
        client = self.get_object()
        raw = (request.data.get("password") or "").strip()
        if raw and len(raw) < MIN_PORTAL_PASSWORD:
            return Response(
                {"detail": f"Пароль минимум {MIN_PORTAL_PASSWORD} символа."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not raw:
            # 6 цифр: админ диктует пароль голосом, буквы и регистр тут только
            # мешают. secrets, а не random — пароль всё-таки.
            raw = f"{secrets.randbelow(1_000_000):06d}"
        client.set_password(raw)
        client.save(update_fields=["portal_password"])
        AuditLog.record(request.user, f"Выдан пароль кабинета клиенту «{client.display_name}»")
        return Response({"password": raw})

    @action(detail=True, methods=["get"], url_path="merge-preview", permission_classes=[IsAdmin])
    def merge_preview(self, request, pk=None):
        """GET /clients/<id>/merge-preview/?from=<id> — что переедет при склейке.

        Удаление карточки необратимо, поэтому объём показываем ДО подтверждения.
        """
        keep = self.get_object()
        drop = Client.objects.filter(pk=request.query_params.get("from")).first()
        if drop is None:
            return Response({"detail": "Карточка не найдена."}, status=status.HTTP_404_NOT_FOUND)
        if drop.pk == keep.pk:
            return Response(
                {"detail": "Нельзя объединить карточку с ней же."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(merge_summary(keep, drop))

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def merge(self, request, pk=None):
        """POST /clients/<id>/merge/ {"from": <id>} — склеить двойников.

        Один человек, заведённый дважды (номер записали в разном формате), имел
        две карточки, и его заказы с долгом лежали двумя стопками. Всё
        переезжает на ЭТУ карточку, вторая удаляется.

        Необратимо и трогает чужие заказы — поэтому только админ.
        """
        keep = self.get_object()
        drop = Client.objects.filter(pk=request.data.get("from")).first()
        if drop is None:
            return Response({"detail": "Карточка не найдена."}, status=status.HTTP_404_NOT_FOUND)

        summary = merge_summary(keep, drop)
        try:
            keep = merge_clients(keep, drop, user=request.user)
        except MergeRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        AuditLog.record(
            request.user,
            f"Объединены карточки клиентов: «{summary['drop']}» ({summary['drop_phone']}) "
            f"→ «{summary['keep']}»; перенесено заказов: {summary['orders']}",
        )
        return Response(
            self.get_serializer(keep).data | {"merged": summary}
        )

    # Деньги — за админом, как и оплата по отдельному чеку.
    @action(detail=True, methods=["post"], url_path="pay-debt", permission_classes=[IsAdmin])
    def pay_debt(self, request, pk=None):
        """POST /clients/<id>/pay-debt/ — общая выплата за несколько заказов.

        Клиент приходит и отдаёт деньги «за всё», а не по одному чеку: одна
        сумма гасит долги его заказов от старых к новым. Раньше это приходилось
        разносить руками, открывая каждый заказ отдельно.

        Тело: `amount` (пусто — закрыть выбранные заказы целиком),
        `receipt_ids` (пусто — все заказы с долгом), `paid_on` (можно задним
        числом), `method`. Возвращает, куда именно ушли деньги.
        """
        from sales.sale_service import (
            PaymentRejected,
            parse_amount,
            parse_paid_on,
            pay_client_debt,
        )

        client = self.get_object()
        # Общая выплата задним числом в закрытый месяц не пускается: она
        # раскидывается по заказам и двигает их долги. Разбор даты оставляем
        # внутри try — кривая дата это тоже 400, а не пятисотка.
        try:
            paid_on = parse_paid_on(request.data.get("paid_on"))
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        ensure_open(paid_on or timezone.localdate(), "Принять выплату этой датой")

        raw_ids = request.data.get("receipt_ids")
        if raw_ids in (None, "", []):
            receipt_ids = None
        elif isinstance(raw_ids, (list, tuple)):
            receipt_ids = raw_ids
        else:
            return Response(
                {"detail": "Некорректный список заказов."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            allocations, change = pay_client_debt(
                client,
                parse_amount(request.data.get("amount")),
                receipt_ids=receipt_ids,
                user=request.user,
                paid_on=paid_on,
                method=request.data.get("method") or None,
            )
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        paid = sum((amount for _, amount in allocations), Decimal("0"))
        numbers = ", ".join(f"№{r.order_number}" for r, _ in allocations)
        AuditLog.record(
            request.user,
            f"Общая выплата клиента «{client.display_name}»: +{paid} сом ({numbers})",
        )

        # Долг пересчитываем СВЕЖИМ запросом: prefetch снят до оплаты и отдал бы
        # старую цифру.
        left = sum((r.debt for r in client.receipts.order_by("created_at")), Decimal("0"))

        # Одно сообщение на всю выплату, а не по штуке на каждый заказ: клиент
        # заплатил один раз, и пять уведомлений подряд выглядят сбоем.
        if client.telegram_chat_id:
            from integrations.telegram import notify_customer

            tail = (
                "Долгов больше нет. Спасибо!"
                if left <= 0
                else f"Остаток долга: {left} сом."
            )
            notify_customer(
                client, f"💰 Принята оплата {paid} сом за заказы {numbers}. {tail}"
            )

        return Response(
            {
                "paid": paid,
                # Сдача: принесли больше, чем висело долга. В долг не пишем —
                # лишнее отдают на руки (то же правило, что в кассе).
                "change": change,
                "debt": left,
                "allocations": [
                    {
                        "receipt": str(r.id),
                        "order_number": r.order_number,
                        "title": r.title,
                        "amount": amount,
                        "debt_after": r.debt,
                    }
                    for r, amount in allocations
                ],
            }
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="request-referral-change",
        permission_classes=[IsAuthenticated],
    )
    def request_referral_change(self, request, pk=None):
        """File a request to change this client's referrer (admin approves)."""
        client = self.get_object()

        raw = request.data.get("referred_by", None)
        target = None
        if raw not in (None, "", "null"):
            target = Client.objects.filter(pk=raw).first()
            if target is None:
                return Response(
                    {"referred_by": "Клиент не найден."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if target.pk == client.pk:
                return Response(
                    {"referred_by": "Клиент не может привести сам себя."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        target_id = target.pk if target else None
        if target_id == client.referred_by_id:
            return Response(
                {"referred_by": "Это значение уже выбрано как реферер."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if client.referral_requests.filter(
            status=ReferralChangeRequest.Status.PENDING
        ).exists():
            return Response(
                {"detail": "По этому клиенту уже есть заявка на рассмотрении."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        req = ReferralChangeRequest.objects.create(
            client=client,
            new_referred_by=target,
            previous_referred_by=client.referred_by,
            requested_by=request.user,
            reason=(request.data.get("reason") or "").strip(),
        )
        AuditLog.record(
            request.user,
            f"Заявка на смену реферера клиента «{client.display_name}» → "
            f"«{target.display_name if target else '—'}»",
        )
        return Response(
            ReferralChangeRequestSerializer(req).data,
            status=status.HTTP_201_CREATED,
        )


class ReferralChangeRequestViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """Moderation queue for referral-change requests (admin only)."""

    queryset = ReferralChangeRequest.objects.select_related(
        "client", "new_referred_by", "previous_referred_by", "requested_by", "reviewed_by"
    )
    serializer_class = ReferralChangeRequestSerializer
    permission_classes = [IsAdmin]
    filterset_fields = ["status", "client"]
    ordering = ["-created_at"]

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        req = self.get_object()
        if req.status != ReferralChangeRequest.Status.PENDING:
            return Response(
                {"detail": "Заявка уже рассмотрена."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        client = req.client
        client.referred_by = req.new_referred_by
        client.save(update_fields=["referred_by"])

        req.status = ReferralChangeRequest.Status.APPROVED
        req.reviewed_by = request.user
        req.reviewed_at = timezone.now()
        req.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        AuditLog.record(
            request.user,
            f"Одобрена смена реферера клиента «{client.display_name}» → "
            f"«{req.new_referred_by.display_name if req.new_referred_by else '—'}»",
        )
        return Response(ReferralChangeRequestSerializer(req).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        req = self.get_object()
        if req.status != ReferralChangeRequest.Status.PENDING:
            return Response(
                {"detail": "Заявка уже рассмотрена."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reason = (request.data.get("reason") or "").strip()
        req.status = ReferralChangeRequest.Status.REJECTED
        req.reviewed_by = request.user
        req.reviewed_at = timezone.now()
        if reason:
            req.reason = reason
        req.save(update_fields=["status", "reviewed_by", "reviewed_at", "reason"])

        AuditLog.record(
            request.user,
            f"Отклонена смена реферера клиента «{req.client.display_name}»",
        )
        return Response(ReferralChangeRequestSerializer(req).data)
