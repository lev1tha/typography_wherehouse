from decimal import Decimal

from rest_framework import serializers

from .models import PricingSettings, PrintingService, ServiceRecipe


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


class PrintingServiceSerializer(serializers.ModelSerializer):
    recipes = ServiceRecipeSerializer(many=True, read_only=True)
    uses_area = serializers.BooleanField(read_only=True)
    uses_material = serializers.BooleanField(read_only=True)
    uses_running_meter = serializers.BooleanField(read_only=True)
    uses_pieces = serializers.BooleanField(read_only=True)
    # Отходы: мерку строки (кв.м / пог.м / шт) выбирают в кассе.
    uses_free_measure = serializers.BooleanField(read_only=True)

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
            "uses_area",
            "uses_material",
            "uses_running_meter",
            "uses_pieces",
            "uses_free_measure",
            "is_active",
            "recipes",
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

    class Meta:
        model = PricingSettings
        fields = ["master_commission_percent", "min_line_amount", "urgency_percent", "updated_at"]
        read_only_fields = ["updated_at"]


class PublicPricingRulesSerializer(serializers.ModelSerializer):
    """То, что касса должна знать о правилах прайса, — всем сотрудникам.

    `master_commission_percent` сюда не входит: доля мастера — только админу.
    """

    class Meta:
        model = PricingSettings
        fields = ["min_line_amount", "urgency_percent"]
