from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _


class Expense(models.Model):
    """A variable cost / investment line — cutter consumables, equipment,
    workshop improvement, other. Feeds the «Переменные расходы» section of the
    financial report and is listed on the «Расходники/Инвестиции» page."""

    class Category(models.TextChoices):
        CUTTER = "CUTTER", _("Расходники фреза")
        EQUIPMENT = "EQUIPMENT", _("Покупка оборудования")
        IMPROVEMENT = "IMPROVEMENT", _("Улучшение цеха")
        OTHER = "OTHER", _("Прочие расходы")

    category = models.CharField(max_length=20, choices=Category.choices)
    name = models.CharField(_("название"), max_length=255, blank=True)
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2, default=Decimal("0"))
    spent_at = models.DateField(_("дата"), auto_now_add=True)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="expenses"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("расход/инвестиция")
        verbose_name_plural = _("расходы/инвестиции")
        ordering = ["-spent_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.get_category_display()}: {self.name} — {self.amount}"


class FinanceSettings(models.Model):
    """Singleton of manual P&L inputs that are not itemised expenses: material
    balances / purchase / transport / supplier-debt and fixed monthly costs.
    Computed values (stock-end, variable costs, revenue, profit) are NOT stored —
    they are calculated live in the report endpoint."""

    # Материалы
    stock_start = models.DecimalField(_("остаток материалов на начало"), max_digits=14, decimal_places=2, default=Decimal("0"))
    material_purchase = models.DecimalField(_("закуп материала"), max_digits=14, decimal_places=2, default=Decimal("0"))
    transport = models.DecimalField(_("транспортные расходы"), max_digits=14, decimal_places=2, default=Decimal("0"))
    material_debt = models.DecimalField(_("долг материала"), max_digits=14, decimal_places=2, default=Decimal("0"))
    # Постоянные расходы
    rent = models.DecimalField(_("аренда цеха"), max_digits=14, decimal_places=2, default=Decimal("0"))
    utilities = models.DecimalField(_("коммунальные услуги"), max_digits=14, decimal_places=2, default=Decimal("0"))
    utilities_note = models.CharField(_("что входит в коммуналку"), max_length=500, blank=True, default="")
    internet = models.DecimalField(_("интернет"), max_digits=14, decimal_places=2, default=Decimal("0"))
    salary = models.DecimalField(_("зарплаты за месяц"), max_digits=14, decimal_places=2, default=Decimal("0"))
    fixed_other = models.DecimalField(_("прочие постоянные расходы"), max_digits=14, decimal_places=2, default=Decimal("0"))
    fixed_other_note = models.CharField(_("что входит в прочие"), max_length=500, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("настройки финотчёта")
        verbose_name_plural = _("настройки финотчёта")

    def __str__(self) -> str:
        return "Настройки финотчёта"

    @classmethod
    def load(cls) -> "FinanceSettings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
