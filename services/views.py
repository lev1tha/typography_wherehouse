from rest_framework import generics, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin, IsAdminOrReadOnly
from audit.models import AuditLog

from .models import PricingSettings, PrintingService, ServiceRecipe
from .serializers import (
    PricingSettingsSerializer,
    PublicPricingRulesSerializer,
    PrintingServiceSerializer,
    ServiceRecipeSerializer,
)


class PrintingServiceViewSet(viewsets.ModelViewSet):
    """Services & pricing. Admin edits base price and paper/cardboard markups."""

    queryset = PrintingService.objects.prefetch_related("recipes__material").all()
    serializer_class = PrintingServiceSerializer
    permission_classes = [IsAdminOrReadOnly]
    search_fields = ["name"]
    ordering = ["name"]

    def perform_update(self, serializer):
        old = PrintingService.objects.get(pk=serializer.instance.pk)
        service = serializer.save()
        if old.base_price != service.base_price:
            AuditLog.record(
                self.request.user,
                f"Изменена базовая цена «{service.name}»: "
                f"{old.base_price} → {service.base_price} сом",
            )
        if old.min_line_amount != service.min_line_amount:
            show = lambda v: "общий" if v is None else f"{v} сом"  # noqa: E731
            AuditLog.record(
                self.request.user,
                f"Изменён минимум строки «{service.name}»: "
                f"{show(old.min_line_amount)} → {show(service.min_line_amount)}",
            )


class ServiceRecipeViewSet(viewsets.ModelViewSet):
    """Technological cards — consumption norms per service unit (admin only)."""

    queryset = ServiceRecipe.objects.select_related("service", "material").all()
    serializer_class = ServiceRecipeSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["service", "material"]


class PricingSettingsView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/services/settings/ — shop-wide pricing settings (admin only)."""

    serializer_class = PricingSettingsSerializer
    permission_classes = [IsAdmin]

    def get_object(self):
        return PricingSettings.load()

    def perform_update(self, serializer):
        before = self.get_object()
        old = before.master_commission_percent
        old_min, old_urgency = before.min_line_amount, before.urgency_percent
        obj = serializer.save()
        if old != obj.master_commission_percent:
            AuditLog.record(
                self.request.user,
                f"Изменён % ЗП мастера: {old} → {obj.master_commission_percent}%",
            )
        if old_min != obj.min_line_amount:
            AuditLog.record(
                self.request.user,
                f"Изменён минимум строки услуги: {old_min} → {obj.min_line_amount} сом",
            )
        if old_urgency != obj.urgency_percent:
            AuditLog.record(
                self.request.user,
                f"Изменена наценка за срочность: {old_urgency} → {obj.urgency_percent}%",
            )


class PricingRulesView(APIView):
    """GET /api/services/rules/ — правила прайса для кассы (любой сотрудник):
    общий минимум строки услуги и наценка за срочность. Правит их админ через
    `/api/services/settings/`."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(PublicPricingRulesSerializer(PricingSettings.load()).data)
