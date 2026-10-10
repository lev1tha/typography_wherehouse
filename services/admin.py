from django.contrib import admin
from modeltranslation.admin import TranslationAdmin

from .models import (
    PricingSettings,
    PrintingService,
    RateMatrixEntry,
    ServiceRecipe,
    ThicknessCoefficient,
)


class ServiceRecipeInline(admin.TabularInline):
    model = ServiceRecipe
    extra = 1


@admin.register(PrintingService)
class PrintingServiceAdmin(TranslationAdmin):
    list_display = ("name", "kind", "base_price", "rate_flat", "rate_per_piece", "is_active")
    inlines = [ServiceRecipeInline]


@admin.register(PricingSettings)
class PricingSettingsAdmin(admin.ModelAdmin):
    list_display = ("master_commission_percent", "updated_at")


@admin.register(ThicknessCoefficient)
class ThicknessCoefficientAdmin(admin.ModelAdmin):
    list_display = ("kind", "thickness_from", "coefficient")


@admin.register(RateMatrixEntry)
class RateMatrixEntryAdmin(admin.ModelAdmin):
    list_display = ("service", "material", "thickness_from", "rate")
