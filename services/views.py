from rest_framework import generics, viewsets

from accounts.permissions import IsAdmin, IsAdminOrReadOnly
from audit.models import AuditLog

from .models import PricingSettings, PrintingService, ServiceRecipe
from .serializers import (
    PricingSettingsSerializer,
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
        old = self.get_object().master_commission_percent
        obj = serializer.save()
        if old != obj.master_commission_percent:
            AuditLog.record(
                self.request.user,
                f"Изменён % ЗП мастера: {old} → {obj.master_commission_percent}%",
            )
