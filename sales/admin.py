from django.contrib import admin

from .models import Receipt, TransactionItem


class TransactionItemInline(admin.TabularInline):
    model = TransactionItem
    extra = 0


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "client",
        "cashier",
        "payment_method",
        "payment_status",
        "status",
        "total_price",
        "created_at",
    )
    list_filter = ("payment_method", "payment_status", "status")
    inlines = [TransactionItemInline]
