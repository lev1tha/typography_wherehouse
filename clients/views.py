from django.db.models import Value
from django.db.models.functions import Coalesce, Lower, NullIf
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdmin
from audit.models import AuditLog

from .models import Client, ReferralChangeRequest
from .serializers import (
    ClientDetailSerializer,
    ClientSerializer,
    ReferralChangeRequestSerializer,
)


class ClientViewSet(viewsets.ModelViewSet):
    """CRM. Live ?search= lookup (по ИМЕНИ или телефону) для быстрого автозаполнения
    в кассе. Поиск регистронезависимый на любой БД (фильтрация в Python — SQLite не
    умеет регистронезависимый LIKE для кириллицы)."""

    queryset = Client.objects.all()
    permission_classes = [IsAuthenticated]
    filterset_fields = ["type"]
    # search_fields НЕ задаём: DRF SearchFilter использует icontains, который на
    # SQLite не находит кириллицу в другом регистре. Ищем сами в get_queryset.
    ordering = ["sort_name"]

    def get_queryset(self):
        # Сортировка по ИМЕНИ (компания или ФИО), с откатом на телефон — не по дате.
        qs = (
            Client.objects.annotate(
                sort_name=Lower(
                    Coalesce(NullIf("company_name", Value("")), NullIf("full_name", Value("")), "phone")
                )
            )
            .prefetch_related("receipts")
            .order_by("sort_name")
        )
        search = (self.request.query_params.get("search") or "").strip().lower()
        if search:
            ids = [
                c.id
                for c in Client.objects.only("id", "full_name", "company_name", "phone")
                if search in (c.full_name or "").lower()
                or search in (c.company_name or "").lower()
                or search in (c.phone or "").lower()
            ]
            qs = qs.filter(id__in=ids)
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
