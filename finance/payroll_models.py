"""Ведомость зарплаты: правила оплаты, начисления, выплаты, удержания.

Раньше зарплата была строкой расхода «Зарплаты» с именем текстом: ни оклада, ни
процента по виду работ, ни аванса, ни остатка «к выдаче». Теперь (2026-10-10,
аудит STAFF-01/-09/-10, cash-11) у каждого сотрудника (`accounts.Employee`):

- ПРАВИЛА ОПЛАТЫ с датой начала (`PayScheme` + `PayRate`): оклад, процент от
  выработки по видам работ и станкам, премия за выработку выше порога. Новое
  правило действует с первого числа своего месяца и прошлые не пересчитывает;
- НАЧИСЛЕНИЕ за месяц (`PayrollAccrual`): «начислено, не выплачено». Попадает в
  ОПиУ месяца (записью `ExpenseEntry` без денег), в кассу не идёт;
- ВЫПЛАТЫ и АВАНСЫ (`PayrollPayment`): привязаны к сотруднику и к месяцу, за
  который платят; пишут кассу (статья «Выплата зарплаты по ведомости») и гасят
  начисление, в ОПиУ не идут;
- УДЕРЖАНИЯ (`PayrollAdjustment`): штраф, брак (со ссылкой на списание склада).
  Уменьшают расход на зарплату: рабочий вернул цеху ущерб.

Расчёт — в `finance.payroll`, здесь только таблицы.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


def _money(label, **kwargs):
    kwargs.setdefault("default", Decimal("0"))
    return models.DecimalField(label, max_digits=14, decimal_places=2, **kwargs)


class PayScheme(models.Model):
    """Правила оплаты сотрудника, действующие с месяца `valid_from`.

    История, а не одна строка (STAFF-05): ставку подняли с октября — сентябрь
    считается по прежней. Для месяца действует запись с наибольшим
    `valid_from` не позже его первого числа.
    """

    class BonusMetric(models.TextChoices):
        RUNNING_METERS = "RUNNING_METERS", _("Погонные метры резки")
        WORK_AMOUNT = "WORK_AMOUNT", _("Сумма выполненных работ, сом")

    employee = models.ForeignKey(
        "accounts.Employee", on_delete=models.CASCADE, related_name="pay_schemes",
        verbose_name=_("сотрудник"),
    )
    valid_from = models.DateField(_("действует с месяца"))
    salary = _money(_("оклад в месяц"), validators=[MinValueValidator(Decimal("0"))])
    bonus_metric = models.CharField(
        _("чем мерится выработка для премии"), max_length=20,
        choices=BonusMetric.choices, default=BonusMetric.RUNNING_METERS,
    )
    bonus_threshold = _money(
        _("порог выработки для премии"), null=True, blank=True, default=None,
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("Выработка за месяц СТРОГО больше порога — премия. Пусто — без премии."),
    )
    bonus_amount = _money(_("премия"), validators=[MinValueValidator(Decimal("0"))])
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pay_schemes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("правила оплаты")
        verbose_name_plural = _("правила оплаты")
        ordering = ["employee_id", "-valid_from"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "valid_from"], name="payscheme_employee_from_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} с {self.valid_from:%m.%Y}"

    def save(self, *args, **kwargs):
        from .periods import month_start

        self.valid_from = month_start(self.valid_from)
        super().save(*args, **kwargs)


class PayRate(models.Model):
    """Процент от выработки по виду работ: «6 % с резки, 10 % с монтажа».

    `work` — вид работы; у резки можно задать общий процент (`CUTTING`) и/или
    свой на станок (`CUTTING_CNC`, `CUTTING_LASER`) — станочный побеждает.
    """

    class Work(models.TextChoices):
        CUTTING = "CUTTING", _("Резка (любой станок)")
        CUTTING_CNC = "CUTTING_CNC", _("Резка на ЧПУ")
        CUTTING_LASER = "CUTTING_LASER", _("Резка на лазере")
        ENGRAVING = "ENGRAVING", _("Гравировка")
        INSTALL = "INSTALL", _("Монтаж и установка")
        OTHER = "OTHER", _("Прочие работы")

    scheme = models.ForeignKey(PayScheme, on_delete=models.CASCADE, related_name="rates")
    work = models.CharField(_("вид работы"), max_length=20, choices=Work.choices)
    percent = models.DecimalField(
        _("процент от стоимости работы"), max_digits=5, decimal_places=2,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )

    class Meta:
        verbose_name = _("процент от работы")
        verbose_name_plural = _("проценты от работы")
        ordering = ["scheme_id", "work"]
        constraints = [
            models.UniqueConstraint(fields=["scheme", "work"], name="payrate_scheme_work_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.get_work_display()}: {self.percent} %"


class PayrollAdjustment(models.Model):
    """Удержание из зарплаты: штраф или брак. Уменьшает «к выдаче» и расход."""

    class Reason(models.TextChoices):
        DEFECT = "DEFECT", _("За брак")
        FINE = "FINE", _("Штраф")
        OTHER = "OTHER", _("Прочее удержание")

    employee = models.ForeignKey(
        "accounts.Employee", on_delete=models.PROTECT, related_name="payroll_adjustments",
        verbose_name=_("сотрудник"),
    )
    month = models.DateField(_("за месяц"))
    reason = models.CharField(_("причина"), max_length=10, choices=Reason.choices, default=Reason.FINE)
    amount = _money(_("сумма"), validators=[MinValueValidator(Decimal("0.01"))])
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    # Брак: ссылка на списание склада, из-за которого удерживают.
    inventory_log = models.ForeignKey(
        "warehouse.InventoryLog", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_adjustments", verbose_name=_("списание"),
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_adjustments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("удержание")
        verbose_name_plural = _("удержания")
        ordering = ["-month", "employee_id", "-created_at"]

    def __str__(self) -> str:
        return f"{self.employee}: {self.get_reason_display()} {self.amount}"

    def save(self, *args, **kwargs):
        from .periods import month_start

        self.month = month_start(self.month)
        super().save(*args, **kwargs)


class PayrollAccrual(models.Model):
    """Начислено за месяц: «Азамату за октябрь 28 910, из них удержано 1 411».

    `gross` — оклад + проценты + премия; `deductions` — удержания месяца на
    момент проведения; `amount` = gross − deductions — это и есть расход на
    зарплату месяца, он лежит в ОПиУ записью `expense` (без денег). Проведение
    можно повторить (пересчёт), пока месяц не закрыт замком периода.
    `breakdown` — снимок расчёта (оклад, строки по видам работ, премия), чтобы
    показать «из чего сложилось» без повторного расчёта.
    """

    employee = models.ForeignKey(
        "accounts.Employee", on_delete=models.PROTECT, related_name="payroll_accruals",
        verbose_name=_("сотрудник"),
    )
    month = models.DateField(_("за месяц"))
    gross = _money(_("начислено"))
    deductions = _money(_("удержано"))
    amount = _money(_("к оплате за месяц"))
    breakdown = models.JSONField(_("расчёт"), default=dict, blank=True)
    expense = models.OneToOneField(
        "finance.ExpenseEntry", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_accrual", verbose_name=_("расход в ОПиУ"),
    )
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_accruals",
    )
    posted_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("начисление зарплаты")
        verbose_name_plural = _("начисления зарплаты")
        ordering = ["-month", "employee_id"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "month"], name="payrollaccrual_employee_month_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.employee} за {self.month:%m.%Y}: {self.amount}"

    def save(self, *args, **kwargs):
        from .periods import month_start

        self.month = month_start(self.month)
        super().save(*args, **kwargs)


class PayrollPayment(models.Model):
    """Аванс или расчёт сотруднику — деньги из кассы, за конкретный месяц."""

    class Kind(models.TextChoices):
        ADVANCE = "ADVANCE", _("Аванс")
        PAYOUT = "PAYOUT", _("Выплата")

    employee = models.ForeignKey(
        "accounts.Employee", on_delete=models.PROTECT, related_name="payroll_payments",
        verbose_name=_("сотрудник"),
    )
    period = models.DateField(_("за какой месяц"))
    kind = models.CharField(_("вид"), max_length=10, choices=Kind.choices, default=Kind.PAYOUT)
    amount = _money(_("сумма"), validators=[MinValueValidator(Decimal("0.01"))])
    paid_on = models.DateField(_("дата выплаты"))
    account = models.CharField(
        _("чем заплатили"), max_length=10, choices=[("CASH", _("Наличные")), ("BANK", _("Банк"))],
        default="CASH",
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    cash_entry = models.OneToOneField(
        "finance.CashEntry", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_payment", verbose_name=_("запись кассы"),
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("выплата зарплаты")
        verbose_name_plural = _("выплаты зарплаты")
        ordering = ["-paid_on", "-created_at"]
        indexes = [models.Index(fields=["period"], name="payrollpay_period_idx")]

    def __str__(self) -> str:
        return f"{self.employee}: {self.get_kind_display()} {self.amount} за {self.period:%m.%Y}"

    def save(self, *args, **kwargs):
        from .periods import month_start

        self.period = month_start(self.period)
        super().save(*args, **kwargs)
