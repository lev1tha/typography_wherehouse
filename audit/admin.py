from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action")
    search_fields = ("action",)
    list_filter = ("user",)

    # Журнал действий — улика: через админку его можно только читать. Добавить,
    # поправить или стереть запись задним числом нельзя никому, включая
    # суперпользователя.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
