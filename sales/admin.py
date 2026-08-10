from django.contrib import admin

from .models import Payment, Receipt, TransactionItem


class TransactionItemInline(admin.TabularInline):
    model = TransactionItem
    extra = 0


class PaymentInline(admin.TabularInline):
    model = Payment
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
    inlines = [TransactionItemInline, PaymentInline]

    def get_readonly_fields(self, request, obj=None):
        """Дату заказа в админке правит только суперпользователь.

        Заказы задним числом — право админа: дата опорная для всей отчётности.
        В интерфейсе это закрыто проверкой в checkout, но `created_at` перестал
        быть `auto_now_add` и стал обычным редактируемым полем — то есть здесь
        появилась вторая дверь. Сейчас она заперта тем, что у складовщика просто
        нет прав на чеки, но это случайность: выдадут право — и дата станет
        доступной. Запираем явно.
        """
        readonly = super().get_readonly_fields(request, obj)
        if request.user.is_superuser:
            return readonly
        return tuple(readonly) + ("created_at",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("receipt", "amount", "method", "paid_on", "created_by", "created_at")
    list_filter = ("method", "paid_on")
    date_hierarchy = "paid_on"
