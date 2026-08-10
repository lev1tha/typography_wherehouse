from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdmin(BasePermission):
    """Allows access only to authenticated users with the Admin role."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_admin_role)


class IsAdminOrReadOnly(BasePermission):
    """Admins can write; storekeepers (and any authenticated staff) read-only."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.is_admin_role


class IsAdminOrAccountantRead(BasePermission):
    """Деньги: админ читает и правит, бухгалтер только смотрит.

    Ставится на разделы, которые до появления бухгалтера были чисто
    админскими — журнал действий, финансовый отчёт, обзор. Бухгалтер именно
    проверяющий: увидеть он должен всё, тронуть — ничего.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_admin_role:
            return True
        return request.user.is_accountant_role and request.method in SAFE_METHODS


class SeesMoney(BasePermission):
    """Кому вообще открыты денежные экраны: админу и бухгалтеру.

    Отдельно от `IsAdminOrAccountantRead`, потому что снятие финансового пароля
    — это POST, а не чтение: разрешать бухгалтеру только SAFE_METHODS значило бы
    закрыть ему вход в раздел, который ему и открыли.
    """

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.sees_money
        )


class IsNotAccountant(BasePermission):
    """Бухгалтеру закрыта запись там, где пишут остальные (касса, склад, клиенты).

    Отдельным классом, а не проверкой внутри каждой вьюхи: пропустить одну
    проверку в одном месте — значит дать проверяющему завести продажу, и
    незаметно это будет ровно до первой сверки.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return not request.user.is_accountant_role
