from django.db import models
from django.utils.translation import gettext_lazy as _


class AuditLog(models.Model):
    """Hidden trail of staff actions: price changes, cancellations, logins."""

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.TextField(_("действие"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("запись аудита")
        verbose_name_plural = _("аудит-лог")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        who = self.user.username if self.user else "—"
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {who}: {self.action[:60]}"

    @classmethod
    def record(cls, user, action: str) -> "AuditLog":
        """Convenience helper used across views/services to log an action."""
        return cls.objects.create(user=user, action=action)
