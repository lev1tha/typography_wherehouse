from rest_framework.permissions import BasePermission

from .models import ClientSettings


class CanTakeDebt(BasePermission):
    """Кто принимает деньги клиента (оплата долга, аванс).

    Админ — всегда. Складовщик — только если владелец включил
    «складовщик принимает оплату долга» (по умолчанию выключено, CLI-08).
    Бухгалтер — никогда: он проверяет, а не участвует.
    """

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_admin_role:
            return True
        if user.is_accountant_role:
            return False
        return ClientSettings.load().storekeeper_takes_debt
