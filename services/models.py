from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _


class PrintingService(models.Model):
    """A billable service.

    Pricing models:
    - CUTTING  (резка / работа мастера): the master's labour is priced by area
      (кв.м) via `rate_flat`. The cut material is billed as a SEPARATE line at
      sale time, so labour and material revenue stay analytically distinct.
    - INSTALL_INTERIOR: priced by area (кв.м).
    - INSTALL_EXTERIOR: priced per letter/piece.
    - INSTALLATION / OTHER: a fixed `base_price` per order.
    """

    class Kind(models.TextChoices):
        CUTTING = "CUTTING", _("Резка / работа мастера (по кв.м)")
        INSTALL_EXTERIOR = "INSTALL_EXTERIOR", _("Наружная установка (за букву)")
        INSTALL_INTERIOR = "INSTALL_INTERIOR", _("Внутренняя установка (по кв.м)")
        INSTALLATION = "INSTALLATION", _("Установка (фикс)")  # legacy
        OTHER = "OTHER", _("Прочее (фикс)")

    class Machine(models.TextChoices):
        """Станок, на котором режут. Заказчик считает резку двумя категориями —
        ЧПУ и лазер, — а не по материалам: «сколько наработал каждый станок» это
        и есть его вопрос, материал в нём вторичен."""

        CNC = "CNC", _("ЧПУ")
        LASER = "LASER", _("Лазер")

    name = models.CharField(_("название"), max_length=255, default="Резка букв")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CUTTING)
    # Заполняется только у резки. У установки и прочего станка нет.
    machine = models.CharField(
        _("станок"),
        max_length=10,
        choices=Machine.choices,
        blank=True,
        default="",
        help_text=_("Для резки: на каком станке. По нему группируется отчёт"),
    )
    base_price = models.DecimalField(
        _("фиксированная стоимость"), max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text=_("Для установки/прочего — фикс. цена за заказ"),
    )
    rate_flat = models.DecimalField(
        _("ставка работы за кв.м"), max_digits=12, decimal_places=2, default=Decimal("0")
    )
    # Ставка резки самого станка. 0 — станок своей ставки не имеет, тогда берётся
    # ставка МАТЕРИАЛА (`Material.cut_rate_per_pm`), как было до разделения на
    # ЧПУ и лазер. Ставка станка выигрывает, потому что иначе выбор станка не
    # менял бы цену — «выбрал лазер, а сумма та же» выглядит поломкой.
    rate_per_pm = models.DecimalField(
        _("ставка резки, сом/пог.м"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Ставка станка. 0 — берётся ставка материала"),
    )
    rate_per_piece = models.DecimalField(
        _("ставка за букву"), max_digits=12, decimal_places=2, default=Decimal("0")
    )
    # Legacy markups (kept for migration safety; unused by the new flow).
    paper_markup = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("5.00"))
    cardboard_markup = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    is_active = models.BooleanField(_("активна"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("услуга")
        verbose_name_plural = _("услуги")
        ordering = ["name"]

    @property
    def uses_area(self) -> bool:
        """Priced per кв.м with a chosen material (cutting, interior install)."""
        return self.kind in (self.Kind.CUTTING, self.Kind.INSTALL_INTERIOR)

    @property
    def uses_material(self) -> bool:
        """Area services (cutting, interior install) bill the chosen material as
        a separate line for clean work-vs-material analytics."""
        return self.uses_area

    @property
    def uses_running_meter(self) -> bool:
        """Cutting work is priced per running metre (длина реза) at the chosen
        material's own rate; interior install stays priced per кв.м."""
        return self.kind == self.Kind.CUTTING

    @property
    def uses_pieces(self) -> bool:
        """Priced per letter/piece (exterior install)."""
        return self.kind == self.Kind.INSTALL_EXTERIOR

    def __str__(self) -> str:
        return self.name


class ServiceRecipe(models.Model):
    """Technological card: which extra material a service consumes, and how much.

    `applies_to` lets a line apply only to a letter type (e.g. glue only for
    volumetric). `consumption_mode` chooses area-proportional vs fixed-per-order.
    """

    class Applies(models.TextChoices):
        ALL = "ALL", _("Всегда")

    class Mode(models.TextChoices):
        PER_SQM = "PER_SQM", _("На кв.м")
        FIXED = "FIXED", _("Фикс. на заказ")

    service = models.ForeignKey(
        PrintingService, on_delete=models.CASCADE, related_name="recipes"
    )
    material = models.ForeignKey(
        "warehouse.Material", on_delete=models.PROTECT, related_name="recipes"
    )
    consumption_per_unit = models.DecimalField(
        _("норма расхода"),
        max_digits=12,
        decimal_places=3,
        help_text=_("На кв.м: расход на 1 кв.м; Фикс: расход на заказ"),
    )
    applies_to = models.CharField(max_length=20, choices=Applies.choices, default=Applies.ALL)
    consumption_mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.PER_SQM)

    class Meta:
        verbose_name = _("норма расхода")
        verbose_name_plural = _("технологическая карта")
        unique_together = ("service", "material", "applies_to")

    def __str__(self) -> str:
        return f"{self.service.name}: {self.material.name} × {self.consumption_per_unit}"


class PricingSettings(models.Model):
    """Singleton shop-wide pricing settings (one row, pk=1)."""

    master_commission_percent = models.DecimalField(
        _("ЗП мастера, % от работы"),
        max_digits=5,
        decimal_places=2,
        default=Decimal("4"),
        help_text=_("Доля мастера от стоимости работы резки (видна только админу)"),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("настройки ценообразования")
        verbose_name_plural = _("настройки ценообразования")

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "PricingSettings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return f"ЗП мастера {self.master_commission_percent}%"
