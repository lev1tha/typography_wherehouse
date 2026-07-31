from decimal import Decimal

from django.db import models
from django.utils import timezone
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


class FixedExpense(models.Model):
    """Постоянный расход отдельной записью: «Аренда за июль», «Коммуналка».

    Раньше это была одна сумма на каждый вид в настройках, которую отчёт
    пропорционально резал под период. Теперь это записи с датами — видно
    историю по месяцам и что именно входило в каждую трату.
    """

    class Category(models.TextChoices):
        RENT = "RENT", _("Аренда цеха")
        UTILITIES = "UTILITIES", _("Коммунальные услуги")
        INTERNET = "INTERNET", _("Интернет")
        OTHER = "OTHER", _("Прочие постоянные")

    category = models.CharField(max_length=20, choices=Category.choices)
    name = models.CharField(_("за что / период"), max_length=255, blank=True)
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2, default=Decimal("0"))
    # Дату ставит пользователь (в отличие от Expense.spent_at): постоянные
    # расходы часто вносят задним числом — «аренда за прошлый месяц».
    spent_at = models.DateField(_("дата"), default=timezone.localdate)
    note = models.TextField(_("примечание"), blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fixed_expenses",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("постоянный расход")
        verbose_name_plural = _("постоянные расходы")
        ordering = ["-spent_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.get_category_display()}: {self.name} — {self.amount}"


class SalaryPayment(models.Model):
    """Выплата зарплаты конкретному человеку.

    Имя — свободный текст, а не ссылка на User: зарплату получают мастера и
    резчики, у которых нет учётной записи в системе.
    """

    employee = models.CharField(_("сотрудник"), max_length=255)
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2, default=Decimal("0"))
    paid_at = models.DateField(_("дата выплаты"), default=timezone.localdate)
    note = models.TextField(_("примечание"), blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="salary_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("выплата зарплаты")
        verbose_name_plural = _("зарплаты")
        ordering = ["-paid_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.employee}: {self.amount} ({self.paid_at})"


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
    # Постоянные расходы и зарплата переехали в модели FixedExpense и
    # SalaryPayment (записи с датами) — здесь их больше нет.
    # Реферальная программа
    referral_bonus = models.DecimalField(
        _("бонус за приведённого клиента"), max_digits=14, decimal_places=2, default=Decimal("0"),
        help_text=_("Фикс. сумма за каждого приведённого клиента. Только показывается в "
                    "карточке клиента — в расходы автоматически НЕ списывается."),
    )
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
