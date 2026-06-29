from django.contrib import admin
from modeltranslation.admin import TranslationAdmin

from .models import InventoryLog, Material, MaterialImage, Roll


class MaterialImageInline(admin.TabularInline):
    model = MaterialImage
    extra = 1


@admin.register(Material)
class MaterialAdmin(TranslationAdmin):
    list_display = (
        "name", "category", "quantity", "critical_balance",
        "price_per_unit", "price_per_sqm", "piece_price", "cut_rate_per_pm",
    )
    list_filter = ("category",)
    search_fields = ("name",)
    inlines = [MaterialImageInline]


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "type", "material", "quantity_changed", "created_by")
    list_filter = ("type",)


@admin.register(Roll)
class RollAdmin(admin.ModelAdmin):
    list_display = (
        "__str__", "material", "remaining_area", "initial_area",
        "purchase_cost", "markup_percent", "received_at",
    )
    list_filter = ("material",)
