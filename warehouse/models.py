from decimal import Decimal

from django.db import models
from django.utils.translation import gettext_lazy as _


class Material(models.Model):
    """Raw material stocked in the warehouse (paper, ink, cardboard, ...).

    `name` and `category` are registered for translation in translation.py so
    the catalogue can be served in RU / KY / EN.

    Roll materials (``is_roll_material=True``) are received as rolls (lots) and
    sold by area (кв.м). Their ``quantity`` is the sum of remaining roll areas,
    ``price_per_unit`` is the retail price per кв.м, and ``purchase_price`` the
    cost per кв.м. See the ``Roll`` model and warehouse.rolls.
    """

    class Unit(models.TextChoices):
        SQM = "SQM", _("кв.м")
        METER = "METER", _("пог.м")
        PIECE = "PIECE", _("шт")
        KG = "KG", _("кг")
        LITER = "LITER", _("л")

    name = models.CharField(_("название"), max_length=255)
    category = models.CharField(_("категория"), max_length=120, db_index=True)
    unit = models.CharField(
        _("единица измерения"), max_length=10, choices=Unit.choices, default=Unit.PIECE
    )
    is_roll_material = models.BooleanField(
        _("рулонный материал"),
        default=False,
        help_text=_("Приходит рулонами, продаётся по кв.м, списывается из партий"),
    )
    quantity = models.DecimalField(
        _("остаток"), max_digits=12, decimal_places=2, default=Decimal("0")
    )
    critical_balance = models.DecimalField(
        _("критический остаток"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Лимит, ниже которого срабатывает алерт"),
    )
    purchase_price = models.DecimalField(
        _("закупочная цена за единицу"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
    )
    price_per_unit = models.DecimalField(
        _("розничная цена за единицу"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Цена продажи клиенту, напр. 50 сом за 1 метр"),
    )
    price_per_sqm = models.DecimalField(
        _("цена за кв.м"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Цена продажи по площади / вырезки, сом за 1 кв.м"),
    )
    piece_price = models.DecimalField(
        _("цена за штуку (лист/рулон)"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Фикс. цена за целую штуку. 0 — продажа штукой недоступна"),
    )
    piece_area = models.DecimalField(
        _("площадь штуки, кв.м"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Площадь одного листа/рулона — списывается при продаже «целиком»"),
    )
    wholesale_price = models.DecimalField(
        _("оптовая цена за лист"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Цена за лист при продаже от опт. минимума. 0 — опта нет"),
    )
    wholesale_min_qty = models.DecimalField(
        _("опт от, листов"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("2"),
        help_text=_("С какого количества листов включается оптовая цена"),
    )
    cut_rate_per_pm = models.DecimalField(
        _("ставка резки, сом/пог.м"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Стоимость работы резки за погонный метр для этого материала"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("материал")
        verbose_name_plural = _("материалы")
        ordering = ["name"]

    @property
    def is_below_critical(self) -> bool:
        return self.quantity <= self.critical_balance

    @property
    def sqm_price(self) -> Decimal:
        """Retail price per кв.м. Falls back to price_per_unit for area materials
        so existing roll materials keep working before per-sqm prices are set."""
        if self.price_per_sqm:
            return self.price_per_sqm
        return self.price_per_unit if self.is_roll_material else Decimal("0")

    def piece_price_for_qty(self, qty) -> Decimal:
        """Per-sheet price for a whole-sheet sale of ``qty`` sheets. Switches to
        the wholesale price once ``qty`` reaches ``wholesale_min_qty`` (only when
        an admin has set a wholesale price). Otherwise the regular piece price."""
        qty = Decimal(str(qty or 0))
        if (
            self.wholesale_price
            and self.wholesale_min_qty
            and qty >= self.wholesale_min_qty
        ):
            return self.wholesale_price
        return self.piece_price

    @property
    def sheets_remaining(self):
        """Stock expressed in whole sheets (кв.м ÷ площадь листа). None when the
        material isn't measured in sheets (no piece_area set)."""
        if self.piece_area and self.piece_area > 0:
            return (self.quantity / self.piece_area).quantize(Decimal("0.01"))
        return None

    @property
    def stock_value(self) -> Decimal:
        """Unrealised asset value of this material at purchase price."""
        return self.quantity * self.purchase_price

    def __str__(self) -> str:
        return self.name


class MaterialImage(models.Model):
    """Gallery image for a material. One image may be flagged primary."""

    material = models.ForeignKey(
        Material, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(_("изображение"), upload_to="materials/")
    is_primary = models.BooleanField(_("главное фото"), default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("фото материала")
        verbose_name_plural = _("фото материалов")
        ordering = ["-is_primary", "uploaded_at"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Ensure at most one primary image per material.
        if self.is_primary:
            MaterialImage.objects.filter(material=self.material).exclude(
                pk=self.pk
            ).update(is_primary=False)

    def __str__(self) -> str:
        return f"Фото #{self.pk} — {self.material.name}"


class InventoryLog(models.Model):
    """Supply intake and inventory adjustments — the audit trail for stock."""

    class Type(models.TextChoices):
        SUPPLY = "SUPPLY", _("Поступление")
        ADJUSTMENT = "ADJUSTMENT", _("Корректировка/Инвентаризация")
        WRITE_OFF = "WRITE_OFF", _("Списание (порча/брак/утеря)")

    type = models.CharField(max_length=20, choices=Type.choices)
    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="inventory_logs"
    )
    quantity_changed = models.DecimalField(
        _("изменение количества"),
        max_digits=12,
        decimal_places=2,
        help_text=_("Положительное — приход, отрицательное — списание"),
    )
    actual_price = models.DecimalField(
        _("новая цена закупки"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    reason = models.TextField(_("причина"), null=True, blank=True)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("складская операция")
        verbose_name_plural = _("складские операции")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_type_display()} {self.material.name}: {self.quantity_changed}"


class Roll(models.Model):
    """A received lot of an area-material, measured in кв.м.

    A lot arrives either as a roll (width × length) or as a stack of sheets
    (width × height × count). Either way `initial_area` (кв.м) is the source of
    truth for stock. Each lot keeps its own cost and markup, so retail price per
    кв.м is computed per lot. Sales consume area FIFO across lots.
    """

    class Form(models.TextChoices):
        ROLL = "ROLL", _("Рулон")
        SHEET = "SHEET", _("Лист")

    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="rolls"
    )
    code = models.CharField(_("маркировка партии"), max_length=120, blank=True)
    form = models.CharField(max_length=10, choices=Form.choices, default=Form.ROLL)
    # Raw dimensions as entered (for display / audit); area is the source of truth.
    width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    length = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    height = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    sheet_count = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    initial_area = models.DecimalField(
        _("площадь при поступлении, кв.м"), max_digits=12, decimal_places=2
    )
    remaining_area = models.DecimalField(
        _("остаток, кв.м"), max_digits=12, decimal_places=2
    )
    purchase_cost = models.DecimalField(
        _("себестоимость рулона"), max_digits=12, decimal_places=2,
        help_text=_("Полная стоимость закупки рулона"),
    )
    markup_percent = models.DecimalField(
        _("наценка, %"), max_digits=6, decimal_places=2, default=Decimal("20.00")
    )
    received_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="rolls",
    )

    class Meta:
        verbose_name = _("рулон")
        verbose_name_plural = _("рулоны")
        ordering = ["received_at"]  # FIFO

    @property
    def cost_per_sqm(self) -> Decimal:
        if not self.initial_area:
            return Decimal("0")
        return (self.purchase_cost / self.initial_area).quantize(Decimal("0.01"))

    @property
    def price_per_sqm(self) -> Decimal:
        """Retail price per кв.м = cost × (1 + markup%) / area."""
        if not self.initial_area:
            return Decimal("0")
        retail_total = self.purchase_cost * (Decimal("1") + self.markup_percent / Decimal("100"))
        return (retail_total / self.initial_area).quantize(Decimal("0.01"))

    @property
    def is_depleted(self) -> bool:
        return self.remaining_area <= 0

    @property
    def dimensions_label(self) -> str:
        if self.form == self.Form.SHEET:
            dims = f"{self.width or '?'}×{self.height or '?'}"
            return f"Лист {dims}" + (f" ×{self.sheet_count}" if self.sheet_count else "")
        return f"Рулон {self.width or '?'}×{self.length or '?'}м"

    def __str__(self) -> str:
        label = self.code or f"Партия #{self.pk}"
        return f"{label} — {self.material.name}: {self.remaining_area}/{self.initial_area} кв.м"
