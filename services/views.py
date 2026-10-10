from decimal import Decimal

from rest_framework import generics, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin, IsAdminOrReadOnly
from audit.models import AuditLog

from .models import (
    PricingSettings,
    PrintingService,
    RateMatrixEntry,
    ServiceRecipe,
    ThicknessCoefficient,
)
from .pricing import resolve_rate
from .serializers import (
    PricingSettingsSerializer,
    PublicPricingRulesSerializer,
    PrintingServiceSerializer,
    RateMatrixSerializer,
    ServiceRecipeSerializer,
    ThicknessCoefficientSerializer,
)


def _show(value) -> str:
    """Значение для журнала: без хвоста нулей, булевы и пустое словами."""
    if value is None or value == "":
        return "—"
    if value is True:
        return "да"
    if value is False:
        return "нет"
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
        return text
    return str(value)


def log_changes(user, subject: str, labels: dict, old: dict, new: dict) -> None:
    """Записать в журнал «было → стало» по каждому изменённому полю.

    Правки цен, ставок и правил прайса раньше оставляли след не всегда: из
    нескольких денежных правок в журнал попадала одна. Теперь любая правка
    поля из `labels` — одна запись на изменение (XL-07 / F3).
    """
    for key, label in labels.items():
        if old.get(key) != new.get(key):
            AuditLog.record(
                user, f"{subject}: {label} {_show(old.get(key))} → {_show(new.get(key))}"
            )


SERVICE_LABELS = {
    "name": "название",
    "kind": "вид",
    "machine": "станок",
    "base_price": "базовая цена",
    "rate_flat": "ставка за кв.м",
    "rate_per_pm": "ставка за пог.м",
    "rate_per_piece": "ставка за букву",
    "min_line_amount": "минимум строки (пусто — общий)",
    "negotiable_price": "цена по договорённости",
    "is_active": "активна",
}

SETTINGS_LABELS = {
    "master_commission_percent": "% ЗП мастера",
    "min_line_amount": "минимум строки услуги",
    "urgency_percent": "наценка за срочность, %",
    "min_mode": "к чему применять минимум",
    "rounding_mode": "округление",
    "confirm_line_total": "порог подтверждения суммы строки",
    "staff_line_cap": "потолок строки для складовщика",
    "staff_min_price_percent": "нижняя граница цены складовщика, %",
    "debt_warn_days": "предупреждать о долге старше, дней",
}


def _snapshot(obj, labels) -> dict:
    return {key: getattr(obj, key) for key in labels}


class PrintingServiceViewSet(viewsets.ModelViewSet):
    """Services & pricing. Admin edits base price and paper/cardboard markups."""

    queryset = PrintingService.objects.prefetch_related(
        "recipes__material", "rate_matrix__material"
    ).all()
    serializer_class = PrintingServiceSerializer
    permission_classes = [IsAdminOrReadOnly]
    search_fields = ["name"]
    ordering = ["name"]

    def perform_create(self, serializer):
        service = serializer.save()
        AuditLog.record(
            self.request.user,
            f"Добавлена услуга «{service.name}» ({service.get_kind_display()}): "
            f"базовая {_show(service.base_price)}, за кв.м {_show(service.rate_flat)}, "
            f"за пог.м {_show(service.rate_per_pm)}, за букву {_show(service.rate_per_piece)}",
        )

    def perform_update(self, serializer):
        old = _snapshot(PrintingService.objects.get(pk=serializer.instance.pk), SERVICE_LABELS)
        service = serializer.save()
        log_changes(
            self.request.user, f"Услуга «{service.name}»", SERVICE_LABELS,
            old, _snapshot(service, SERVICE_LABELS),
        )

    def perform_destroy(self, instance):
        AuditLog.record(self.request.user, f"Удалена услуга «{instance.name}»")
        instance.delete()


class ServiceRecipeViewSet(viewsets.ModelViewSet):
    """Technological cards — consumption norms per service unit (admin only)."""

    queryset = ServiceRecipe.objects.select_related("service", "material").all()
    serializer_class = ServiceRecipeSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["service", "material"]

    def _describe(self, recipe):
        return (
            f"техкарта «{recipe.service.name}»: {recipe.material.name} × "
            f"{_show(recipe.consumption_per_unit)} ({recipe.get_consumption_mode_display()})"
        )

    def perform_create(self, serializer):
        recipe = serializer.save()
        AuditLog.record(self.request.user, f"Добавлена {self._describe(recipe)}")

    def perform_update(self, serializer):
        before = self._describe(self.get_object())
        recipe = serializer.save()
        after = self._describe(recipe)
        if before != after:
            AuditLog.record(self.request.user, f"Изменена {before} → {after}")

    def perform_destroy(self, instance):
        AuditLog.record(self.request.user, f"Удалена {self._describe(instance)}")
        instance.delete()


class RateMatrixViewSet(viewsets.ModelViewSet):
    """Матрица ставок «услуга × материал / толщина → ставка» (CALC-05).
    Читают все сотрудники, правит админ; каждая правка — в журнале."""

    queryset = RateMatrixEntry.objects.select_related("service", "material").all()
    serializer_class = RateMatrixSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["service", "material"]

    def _key(self, entry):
        return entry.material.name if entry.material_id else f"толщина от {_show(entry.thickness_from)} мм"

    def perform_create(self, serializer):
        entry = serializer.save()
        AuditLog.record(
            self.request.user,
            f"Матрица ставок «{entry.service.name}» / {self._key(entry)}: добавлена ставка {_show(entry.rate)}",
        )

    def perform_update(self, serializer):
        old_rate = self.get_object().rate
        entry = serializer.save()
        if old_rate != entry.rate:
            AuditLog.record(
                self.request.user,
                f"Матрица ставок «{entry.service.name}» / {self._key(entry)}: "
                f"ставка {_show(old_rate)} → {_show(entry.rate)}",
            )

    def perform_destroy(self, instance):
        AuditLog.record(
            self.request.user,
            f"Матрица ставок «{instance.service.name}» / {self._key(instance)}: "
            f"убрана ставка {_show(instance.rate)}",
        )
        instance.delete()


class ThicknessCoefficientViewSet(viewsets.ModelViewSet):
    """Коэффициенты по толщине материала (CALC-02): правит админ, читают все."""

    queryset = ThicknessCoefficient.objects.all()
    serializer_class = ThicknessCoefficientSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["kind"]

    def _label(self, row):
        return f"{row.get_kind_display()} от {_show(row.thickness_from)} мм"

    def perform_create(self, serializer):
        row = serializer.save()
        AuditLog.record(
            self.request.user,
            f"Коэффициент толщины ({self._label(row)}): добавлен × {_show(row.coefficient)}",
        )

    def perform_update(self, serializer):
        old = self.get_object().coefficient
        row = serializer.save()
        if old != row.coefficient:
            AuditLog.record(
                self.request.user,
                f"Коэффициент толщины ({self._label(row)}): × {_show(old)} → × {_show(row.coefficient)}",
            )

    def perform_destroy(self, instance):
        AuditLog.record(
            self.request.user,
            f"Коэффициент толщины ({self._label(instance)}): убран × {_show(instance.coefficient)}",
        )
        instance.delete()


class PricingSettingsView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/services/settings/ — shop-wide pricing settings (admin only).

    Неизвестные поля в теле — 400 (CALC-02): раньше `engraving_passes=3`
    принималось и молча терялось, а владелец был уверен, что настройка стоит.
    """

    serializer_class = PricingSettingsSerializer
    permission_classes = [IsAdmin]

    def get_object(self):
        return PricingSettings.load()

    def update(self, request, *args, **kwargs):
        serializer_cls = self.get_serializer_class()
        known = set(serializer_cls().fields) - set(serializer_cls.Meta.read_only_fields)
        unknown = sorted(set(request.data) - known)
        if unknown:
            return Response(
                {
                    "detail": "Неизвестные настройки: " + ", ".join(unknown) + ". Они не сохраняются.",
                    **{key: ["Неизвестная настройка."] for key in unknown},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        old = _snapshot(self.get_object(), SETTINGS_LABELS)
        obj = serializer.save()
        log_changes(self.request.user, "Настройки цен", SETTINGS_LABELS, old, _snapshot(obj, SETTINGS_LABELS))


class PricingRulesView(APIView):
    """GET /api/services/rules/ — правила прайса для кассы (любой сотрудник):
    общий минимум строки услуги, наценка за срочность, режимы, пороги и
    доступность онлайн-оплаты. Правит их админ через `/api/services/settings/`."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(PublicPricingRulesSerializer(PricingSettings.load()).data)


class ResolvedRateView(APIView):
    """GET /api/services/rate/?service=<id>&material=<id> — какая ставка работы
    будет у этой услуги для этого материала (матрица → станок → материал,
    плюс коэффициент по толщине). Касса показывает её в окне резки, не считая
    сама: правила живут в одном месте (`services.pricing.resolve_rate`)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from warehouse.models import Material

        try:
            service = PrintingService.objects.get(pk=request.query_params.get("service"))
        except (PrintingService.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Услуга не найдена."}, status=status.HTTP_404_NOT_FOUND)
        material = None
        raw = request.query_params.get("material")
        if raw:
            try:
                material = Material.objects.get(pk=raw)
            except (Material.DoesNotExist, ValueError):
                return Response({"detail": "Материал не найден."}, status=status.HTTP_404_NOT_FOUND)
        resolved = resolve_rate(service, material)
        return Response({
            "rate": resolved.rate,
            "base": resolved.base,
            "source": resolved.source,
            "coefficient": resolved.coefficient,
        })
