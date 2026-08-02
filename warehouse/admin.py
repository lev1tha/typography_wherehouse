from django.contrib import admin
from modeltranslation.admin import TranslationAdmin

from .models import InventoryLog, Material, MaterialImage, Roll


class MaterialImageInline(admin.TabularInline):
    model = MaterialImage
    extra = 1


@admin.register(Material)
class MaterialAdmin(TranslationAdmin):
    list_display = (
        "name", "type", "thickness_mm", "color", "quantity", "critical_balance",
        "price_per_unit", "price_per_sqm", "piece_price", "cut_rate_per_pm",
    )
    list_filter = ("type", "color")
    search_fields = ("name",)
    inlines = [MaterialImageInline]


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = (
        "happened_at", "type", "material", "quantity_changed", "receipt", "created_by",
    )
    list_filter = ("type", "material")
    search_fields = ("material__name", "reason")
    raw_id_fields = ("receipt",)


@admin.register(Roll)
class RollAdmin(admin.ModelAdmin):
    list_display = (
        "__str__", "material", "remaining_area", "initial_area",
        "purchase_cost", "received_at",
    )
    list_filter = ("material",)
