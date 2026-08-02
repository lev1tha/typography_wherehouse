from django.contrib import admin

from .models import ExpenseEntry, ExpenseKind, FinanceSettings


@admin.register(ExpenseKind)
class ExpenseKindAdmin(admin.ModelAdmin):
    list_display = ("name", "block", "in_profit", "is_builtin", "position", "is_archived")
    list_filter = ("block", "in_profit", "is_builtin", "is_archived")


@admin.register(ExpenseEntry)
class ExpenseEntryAdmin(admin.ModelAdmin):
    list_display = ("kind", "name", "amount", "spent_at")
    list_filter = ("kind",)


@admin.register(FinanceSettings)
class FinanceSettingsAdmin(admin.ModelAdmin):
    pass
