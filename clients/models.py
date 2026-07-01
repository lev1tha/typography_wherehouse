from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils.translation import gettext_lazy as _


class Client(models.Model):
    """Customer — a physical person or an OSOO (company)."""

    class Type(models.TextChoices):
        PHYSICAL = "PHYSICAL", _("Физ. лицо")
        OSOO = "OSOO", _("ОСОО")

    type = models.CharField(max_length=20, choices=Type.choices, default=Type.PHYSICAL)
    full_name = models.CharField(_("ФИО"), max_length=255, null=True, blank=True)
    company_name = models.CharField(
        _("название компании"), max_length=255, null=True, blank=True
    )
    phone = models.CharField(_("телефон"), max_length=32, unique=True)
    # Пароль клиентского портала (хеш). Пусто = ещё не задан: клиент придумает
    # его при первом входе (вход по телефону). Никогда не хранится в открытом виде.
    portal_password = models.CharField(_("пароль портала"), max_length=255, blank=True, default="")
    telegram_chat_id = models.CharField(
        _("Telegram chat id"), max_length=64, null=True, blank=True
    )
    referred_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referrals",
        verbose_name=_("кого привёл"),
        help_text=_("Клиент, который привёл этого клиента"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("клиент")
        verbose_name_plural = _("клиенты")
        ordering = ["-created_at"]

    @property
    def display_name(self) -> str:
        if self.type == self.Type.OSOO:
            return self.company_name or self.phone
        return self.full_name or self.phone

    @property
    def is_telegram_linked(self) -> bool:
        return bool(self.telegram_chat_id)

    @property
    def has_password(self) -> bool:
        return bool(self.portal_password)

    def set_password(self, raw: str) -> None:
        """Store a salted hash of the portal password (never the raw value)."""
        self.portal_password = make_password(raw)

    def check_password(self, raw: str) -> bool:
        return bool(self.portal_password) and check_password(raw, self.portal_password)

    def __str__(self) -> str:
        return f"{self.display_name} ({self.phone})"


class ReferralChangeRequest(models.Model):
    """Заявка на смену реферера (`Client.referred_by`).

    Реферал залочен после установки: кладовщик не может изменить его напрямую,
    но может подать заявку, которую администратор одобряет или отклоняет.
    Администратор также может менять реферера напрямую, минуя очередь.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Ожидает")
        APPROVED = "APPROVED", _("Одобрено")
        REJECTED = "REJECTED", _("Отклонено")

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="referral_requests",
        verbose_name=_("клиент"),
    )
    # null => предложение убрать реферера.
    new_referred_by = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="+",
        null=True,
        blank=True,
        verbose_name=_("новый реферер"),
    )
    # Снимок текущего реферера на момент подачи заявки (для аудита).
    previous_referred_by = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        verbose_name=_("прежний реферер"),
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("кто запросил"),
    )
    reviewed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("кто рассмотрел"),
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(_("обоснование / причина"), blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("заявка на смену реферера")
        verbose_name_plural = _("заявки на смену реферера")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        target = self.new_referred_by.display_name if self.new_referred_by else "—"
        return f"{self.client.display_name} → {target} [{self.get_status_display()}]"
