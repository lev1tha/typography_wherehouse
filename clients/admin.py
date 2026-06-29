from django.contrib import admin

from .models import Client, ReferralChangeRequest


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("display_name", "type", "phone", "is_telegram_linked", "created_at")
    list_filter = ("type",)
    search_fields = ("phone", "full_name", "company_name")


@admin.register(ReferralChangeRequest)
class ReferralChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("client", "new_referred_by", "status", "requested_by", "created_at")
    list_filter = ("status",)
    search_fields = ("client__phone", "client__full_name", "client__company_name")
