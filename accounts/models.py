from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Staff account. Two roles only: Admin and Storekeeper.

    Login uses only username + password (see the auth endpoint); email is
    optional. Roles drive both API permissions and frontend routing.
    """

    class Role(models.TextChoices):
        ADMIN = "ADMIN", _("Администратор")
        STOREKEEPER = "STOREKEEPER", _("Складовщик")

    role = models.CharField(
        _("роль"),
        max_length=20,
        choices=Role.choices,
        default=Role.STOREKEEPER,
    )

    @property
    def is_admin_role(self) -> bool:
        return self.role == self.Role.ADMIN

    def __str__(self) -> str:
        return f"{self.username} ({self.get_role_display()})"


class Profile(models.Model):
    """Extra staff details kept separate from the auth-critical User fields."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile"
    )
    phone = models.CharField(_("телефон"), max_length=32, blank=True)
    telegram_chat_id = models.CharField(
        _("Telegram chat id"), max_length=64, blank=True
    )

    def __str__(self) -> str:
        return f"Профиль {self.user.username}"
