from django.contrib import admin

from .models import (
    BalanceOffset,
    Client,
    ClientAdvance,
    ClientSettings,
    ReferralBonus,
    ReferralChangeRequest,
)


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


# Деньги клиента — только для просмотра в админке: правятся через экран «Клиенты»,
# где работают проверки (замок периода, касса, журнал).
@admin.register(ClientSettings)
class ClientSettingsAdmin(admin.ModelAdmin):
    list_display = ("default_credit_limit", "storekeeper_takes_debt", "updated_at")


@admin.register(ClientAdvance)
class ClientAdvanceAdmin(admin.ModelAdmin):
    list_display = ("client", "amount", "remaining", "method", "paid_on", "reverted_at")
    list_filter = ("method",)
    search_fields = ("client__phone", "client__full_name", "client__company_name")
    readonly_fields = ("client", "amount", "remaining", "method", "paid_on", "created_by", "reverted_at")


@admin.register(BalanceOffset)
class BalanceOffsetAdmin(admin.ModelAdmin):
    list_display = ("client", "source", "amount", "order_number", "used_on")
    list_filter = ("source",)


@admin.register(ReferralBonus)
class ReferralBonusAdmin(admin.ModelAdmin):
    list_display = ("referrer", "referred", "amount", "accrued_on", "paid_amount", "paid_on", "voided_at")
    search_fields = ("referrer__phone", "referred__phone")
