from django.contrib import admin
from modeltranslation.admin import TranslationAdmin

from .models import PricingSettings, PrintingService, ServiceRecipe


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
