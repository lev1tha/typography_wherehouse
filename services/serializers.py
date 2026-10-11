from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from .models import (
    PricingSettings,
    PrintingService,
    RateMatrixEntry,
    ServiceRecipe,
    ThicknessCoefficient,
)


class ServiceRecipeSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)

    class Meta:
        model = ServiceRecipe
        fields = [
            "id",
            "service",
            "material",
            "material_name",
            "consumption_per_unit",
            "applies_to",
            "consumption_mode",
        ]


class RateMatrixSerializer(serializers.ModelSerializer):
    """Ставка услуги для материала или толщины (CALC-05). Ровно один ключ:
    `material` либо `thickness_from`."""

    material_name = serializers.CharField(source="material.name", read_only=True, default=None)
    service_name = serializers.CharField(source="service.name", read_only=True)
    rate = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))
    thickness_from = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True,
    )

    class Meta:
        model = RateMatrixEntry
        fields = [
            "id", "service", "service_name", "material", "material_name",
            "thickness_from", "rate",
        ]
        # Уникальность проверяем сами: условные ограничения базы DRF превращает в
        # «обязательное поле» там, где ключа по смыслу нет.
        validators = []

    def validate(self, attrs):
        material = attrs.get("material", getattr(self.instance, "material", None))
        thickness = attrs.get("thickness_from", getattr(self.instance, "thickness_from", None))
        if (material is None) == (thickness is None):
            raise serializers.ValidationError(
                "Укажите ровно одно: материал или толщину «от» (мм)."
            )
        service = attrs.get("service", getattr(self.instance, "service", None))
        if service is not None and not service.uses_area:
            raise serializers.ValidationError(
                {"service": "Матрица ставок — у резки, гравировки и внутреннего монтажа."}
            )
        siblings = RateMatrixEntry.objects.filter(service=service)
        if self.instance is not None:
            siblings = siblings.exclude(pk=self.instance.pk)
        if material is not None and siblings.filter(material=material).exists():
            raise serializers.ValidationError("Для этой услуги и материала ставка уже задана.")
        if thickness is not None and siblings.filter(thickness_from=thickness).exists():
            raise serializers.ValidationError("Для этой услуги и толщины ставка уже задана.")
        return attrs


class ThicknessCoefficientSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    thickness_from = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=Decimal("0"),
    )
    coefficient = serializers.DecimalField(
        max_digits=6, decimal_places=3, min_value=Decimal("0.001"), max_value=Decimal("100"),
    )

    class Meta:
        model = ThicknessCoefficient
        fields = ["id", "kind", "kind_display", "thickness_from", "coefficient"]

    def validate_kind(self, value):
        area_kinds = (
            PrintingService.Kind.CUTTING, PrintingService.Kind.ENGRAVING,
            PrintingService.Kind.INSTALL_INTERIOR,
        )
        if value not in area_kinds:
            raise serializers.ValidationError(
                "Коэффициент толщины — у резки, гравировки и внутреннего монтажа."
            )
        return value


class PrintingServiceSerializer(serializers.ModelSerializer):
    recipes = ServiceRecipeSerializer(many=True, read_only=True)
    rate_matrix = RateMatrixSerializer(many=True, read_only=True)
    uses_area = serializers.BooleanField(read_only=True)
    uses_material = serializers.BooleanField(read_only=True)
    uses_running_meter = serializers.BooleanField(read_only=True)
    uses_pieces = serializers.BooleanField(read_only=True)
    # Отходы: мерку строки (кв.м / пог.м / шт) выбирают в кассе.
    uses_free_measure = serializers.BooleanField(read_only=True)
    # Складовщик вписывает цену этой услуги в кассе (гравировка, отходы или флаг
    # «по договорённости»).
    staff_sets_rate = serializers.BooleanField(read_only=True)

    machine_display = serializers.CharField(source="get_machine_display", read_only=True)

    class Meta:
        model = PrintingService
        fields = [
            "id",
            "name",
            "kind",
            "machine",
            "machine_display",
            "base_price",
            "rate_flat",
            "rate_per_pm",
            "rate_per_piece",
            # Минимум строки этой услуги: пусто — общий, 0 — без минимума.
            "min_line_amount",
            # Цена по договорённости: её вписывают в кассе (и складовщик).
            "negotiable_price",
            "uses_area",
            "uses_material",
            "uses_running_meter",
            "uses_pieces",
            "uses_free_measure",
            "staff_sets_rate",
            "is_active",
            "recipes",
            "rate_matrix",
            "created_at",
        ]
        read_only_fields = ["created_at"]
        extra_kwargs = {"min_line_amount": {"min_value": Decimal("0")}}


class PricingSettingsSerializer(serializers.ModelSerializer):
    # Правила прайса (2026-10-10): 0 — правило выключено.
    min_line_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False,
    )
    urgency_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("500"),
        required=False,
    )
    confirm_line_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False,
    )
    staff_line_cap = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False,
    )
    staff_min_price_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        required=False,
    )
    staff_price_warn_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        required=False,
    )
    # «ЗП мастера, % от работы» (STAFF-05, волна 2): доля от стоимости работы —
    # 0–100. Раньше проходили −5 и 250: расчётная доля мастера в отчёте
    # становилась отрицательной или больше самой работы. Проверка на уровне
    # API (без миграции модели); то же условие — `PricingSettings.clean`.
    master_commission_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        required=False,
    )

    class Meta:
        model = PricingSettings
        fields = [
            "master_commission_percent", "min_line_amount", "urgency_percent",
            "min_mode", "rounding_mode", "confirm_line_total", "staff_line_cap",
            "staff_min_price_percent", "staff_price_warn_percent", "debt_warn_days", "updated_at",
        ]
        read_only_fields = ["updated_at"]


class PublicPricingRulesSerializer(serializers.ModelSerializer):
    """То, что касса должна знать о правилах прайса, — всем сотрудникам.

    `master_commission_percent` сюда не входит: доля мастера — только админу.
    """

    # ONLINE-оплату скрываем, пока шлюз — заглушка (CLI-09): кнопка, которая ведёт
    # на пустую ссылку, только путает кассира.
    online_payments_enabled = serializers.SerializerMethodField()

    class Meta:
        model = PricingSettings
        fields = [
            "min_line_amount", "urgency_percent", "min_mode", "rounding_mode",
            "confirm_line_total", "staff_line_cap", "staff_min_price_percent",
            "debt_warn_days", "online_payments_enabled",
        ]

    def get_online_payments_enabled(self, obj):
        return (settings.PAYMENT_GATEWAY or "mock").lower() != "mock"
