from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

LOGIN_CODE_TTL_MINUTES = 15


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
    # Одноразовый код входа, который персонал выдаёт клиенту лично (у прилавка) —
    # альтернатива паролю, если клиент забыл его/ещё не задавал. Хранится хешем,
    # как и portal_password; одноразовый и коротко живущий (см. set_login_code).
    login_code = models.CharField(max_length=255, blank=True, default="")
    login_code_expires_at = models.DateTimeField(null=True, blank=True)
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

    def set_login_code(self, raw: str, ttl_minutes: int = LOGIN_CODE_TTL_MINUTES) -> None:
        """Store a salted hash of a staff-issued one-time login code."""
        self.login_code = make_password(raw)
        self.login_code_expires_at = timezone.now() + timedelta(minutes=ttl_minutes)

    def check_login_code(self, raw: str) -> bool:
        """True if `raw` matches the CURRENT, still-valid (unused, unexpired) code."""
        if not self.login_code or not self.login_code_expires_at:
            return False
        if timezone.now() > self.login_code_expires_at:
            return False
        return check_password(raw, self.login_code)

    def clear_login_code(self) -> None:
        """Invalidate the current code right after use. Keeps the hash (only
        backdates the expiry) so a spent code can never later slip through the
        "first login sets your own password" path as a permanent password —
        see `issued_login_code_matches` / CustomerLoginView."""
        self.login_code_expires_at = timezone.now() - timedelta(seconds=1)

    def issued_login_code_matches(self, raw: str) -> bool:
        """True if `raw` equals the most recently issued code, used or not."""
        return bool(self.login_code) and check_password(raw, self.login_code)

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
