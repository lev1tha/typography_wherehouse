from django.contrib import admin

from .models import Expense, FinanceSettings


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("category", "name", "amount", "spent_at")
    list_filter = ("category",)


@admin.register(FinanceSettings)
class FinanceSettingsAdmin(admin.ModelAdmin):
    pass
