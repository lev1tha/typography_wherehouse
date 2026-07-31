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

from accounts.permissions import IsAdmin
from audit.models import AuditLog

from .models import LOGIN_CODE_TTL_MINUTES, Client, ReferralChangeRequest
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
    permission_classes = [IsAuthenticated]
    filterset_fields = ["type"]
    # search_fields НЕ задаём: DRF SearchFilter использует icontains, который на
    # SQLite не находит кириллицу в другом регистре. Ищем сами в get_queryset.
    ordering = ["sort_name"]
    ordering_fields = ["sort_name", "orders_count", "debt", "created_at"]

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
                Q(receipts__payment_status=Receipt.PaymentStatus.PENDING)
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

    @action(detail=True, methods=["post"], url_path="reset-password", permission_classes=[IsAuthenticated])
    def reset_password(self, request, pk=None):
        """Сбросить пароль клиентского портала (клиент забыл). Пароль очищается —
        при следующем входе по телефону клиент задаст новый."""
        client = self.get_object()
        client.portal_password = ""
        client.save(update_fields=["portal_password"])
        AuditLog.record(request.user, f"Сброс пароля клиента {client.display_name}")
        return Response({"ok": True, "has_password": False})

    @action(detail=True, methods=["post"], url_path="issue-login-code", permission_classes=[IsAuthenticated])
    def issue_login_code(self, request, pk=None):
        """Выдать клиенту одноразовый код входа в портал — персонал называет его
        клиенту лично (не через SMS/Telegram), тот вводит код вместо пароля.
        Полезно, если клиент у прилавка и забыл/не задавал пароль. Код
        одноразовый и живёт ограниченное время (см. LOGIN_CODE_TTL_MINUTES)."""
        client = self.get_object()
        code = f"{secrets.randbelow(1_000_000):06d}"
        client.set_login_code(code)
        client.save(update_fields=["login_code", "login_code_expires_at"])
        AuditLog.record(request.user, f"Выдан код входа клиенту «{client.display_name}»")
        return Response({"code": code, "expires_in_minutes": LOGIN_CODE_TTL_MINUTES})

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
