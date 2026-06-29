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

    class Meta:
        model = PrintingService
        fields = [
            "id",
            "name",
            "kind",
            "base_price",
            "rate_flat",
            "rate_per_piece",
            "uses_area",
            "uses_material",
            "uses_running_meter",
            "uses_pieces",
            "is_active",
            "recipes",
            "created_at",
        ]
        read_only_fields = ["created_at"]


class PricingSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PricingSettings
        fields = ["master_commission_percent", "updated_at"]
        read_only_fields = ["updated_at"]
