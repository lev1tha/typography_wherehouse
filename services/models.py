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
    - WASTE (отходы): мерку выбирают в строке — кв.м (`rate_flat`), пог.м
      (`rate_per_pm`) или штуки (`rate_per_piece`). Склада не касается.
    """

    class Kind(models.TextChoices):
        CUTTING = "CUTTING", _("Резка / работа мастера (по кв.м)")
        INSTALL_EXTERIOR = "INSTALL_EXTERIOR", _("Наружная установка (за букву)")
        INSTALL_INTERIOR = "INSTALL_INTERIOR", _("Внутренняя установка (по кв.м)")
        INSTALLATION = "INSTALLATION", _("Установка (фикс)")  # legacy
        OTHER = "OTHER", _("Прочее (фикс)")
        # Гравировка (2026-09-04): цена за кв.м гравируемой площади. Материал
        # отдельной строкой НЕ идёт — гравируют либо материал клиента, либо
        # лист, проданный своей строкой. Цену за кв.м правят в момент продажи и
        # админ, и складовщик: у крупных заказов она своя («5 000 за квадрат»).
        ENGRAVING = "ENGRAVING", _("Гравировка (по кв.м)")
        # Отходы (2026-09-21): цех продаёт обрезки и брак — дёшево и по
        # договорённости. Мерка у КАЖДОЙ строки своя, потому что отходы
        # бывают от любого товара на складе: от листа — квадратами, от
        # рулона — метрами, от штучного — штуками. Поэтому это не «ещё одна
        # площадная услуга», а услуга со СВОБОДНОЙ меркой: её выбирают в
        # кассе, вместе с ценой.
        WASTE = "WASTE", _("Отходы (кв.м / пог.м / шт)")

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
    # Минимальная сумма строки этой услуги (2026-10-10, CALC-01): строка чека,
    # которая по расчёту дешевле, стоит минимум — как `=МАКС(500; расчёт)` в
    # Excel владельца. Пусто — действует общий минимум из настроек цен; 0 — у
    # этой услуги минимума нет, даже если общий задан.
    min_line_amount = models.DecimalField(
        _("минимальная сумма строки"), max_digits=12, decimal_places=2,
        null=True, blank=True,
        help_text=_("Пусто — общий минимум из настроек; 0 — без минимума"),
    )
    # «Цена по договорённости» (2026-10-10, CALC-07): цену за единицу этой
    # услуги вписывают в кассе. Админ вписывает её у любой услуги, складовщик —
    # только у тех, где стоит этот флаг (монтаж, буквы, «Прочее»). Без флага
    # вписанная складовщиком цена — отказ 403, а не молча каталожная.
    negotiable_price = models.BooleanField(
        _("цена по договорённости"), default=False,
        help_text=_("Цену за единицу вписывают в кассе; складовщик — тоже"),
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
        """Priced per кв.м: cutting, interior install, engraving."""
        return self.kind in (self.Kind.CUTTING, self.Kind.INSTALL_INTERIOR, self.Kind.ENGRAVING)

    @property
    def uses_material(self) -> bool:
        """Area services (cutting, interior install) bill the chosen material as
        a separate line for clean work-vs-material analytics. Гравировка —
        нет: у неё площадь — это площадь РИСУНКА, а не куска материала, и
        материал (если он цеха) продаётся своей строкой, как лист под рез."""
        return self.uses_area and self.kind != self.Kind.ENGRAVING

    @property
    def uses_free_measure(self) -> bool:
        """Мерку строки выбирают в кассе: кв.м, пог.м или штуки (отходы).

        Намеренно НЕ входит в `uses_area`: у площадных услуг мерка одна и
        известна заранее, а здесь она у каждой строки своя. Смешать их значило
        бы требовать «ширину × длину» у отходов, которые продают метрами.
        Материала со склада у такой строки нет вовсе — отход уже списан там,
        где его признали браком («Отход (брак)» в приёмке) или продали как
        обрезок резки; второе списание увело бы остаток в минус.
        """
        return self.kind == self.Kind.WASTE

    @property
    def staff_sets_rate(self) -> bool:
        """Ставку этой услуги в момент продажи правит и складовщик, не только
        админ. Ручная цена — право админа (аудит 18.08, п. 14), но гравировку
        владелец попросил сделать правимой у кассы: «для больших заказов цена
        за кв.м будет 5 000». Отходы — тем же решением (2026-09-21): цена на
        них всегда договорная, каталожная тут лишь подсказка. Резка МАТЕРИАЛА
        КЛИЕНТА открыта так же, но это свойство строки (`own_material`), а не
        услуги."""
        return self.kind in (self.Kind.ENGRAVING, self.Kind.WASTE) or self.negotiable_price

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
        # Износ расходника от ДЛИНЫ реза (2026-10-10, PNL-05): фреза и трубка
        # лазера стареют от пог.м, а не от площади куска. У строк без длины
        # реза (не резка) расход по такой норме — ноль.
        PER_PM = "PER_PM", _("На пог.м реза")

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
    # Правила прайса (2026-10-10, CALC-01). Оба по умолчанию 0 — правило
    # выключено, цены считаются как раньше. Применяются построчно при сборке
    # строки чека (`sales.pricing_rules`).
    min_line_amount = models.DecimalField(
        _("минимальная сумма строки услуги"), max_digits=12, decimal_places=2,
        default=Decimal("0"),
        help_text=_("Строка услуги дешевле — стоит этот минимум. 0 — выключено"),
    )
    urgency_percent = models.DecimalField(
        _("наценка за срочность, %"), max_digits=5, decimal_places=2,
        default=Decimal("0"),
        help_text=_("Переключатель «Срочно» в кассе. 0 — выключено"),
    )
    # Режим минимума (2026-10-10, CALC-01 / G4-N2): к чему применяется «минимум
    # строки». «Деталь» — к работе + материалу ОДНОЙ детали (шильдик 508 вместо
    # 500 пропадает: как `=МАКС(500; рез+материал)` в Excel); «работа» — только
    # к строке работы (как было до 10.10); «заказ» — к сумме всего заказа.
    # При минимуме 0 режим ничего не меняет.
    class MinMode(models.TextChoices):
        WORK = "WORK", _("Работа")
        PART = "PART", _("Деталь (работа + материал)")
        ORDER = "ORDER", _("Заказ")

    min_mode = models.CharField(
        _("к чему применять минимум"), max_length=10, choices=MinMode.choices,
        default=MinMode.PART,
    )

    class Rounding(models.TextChoices):
        LINE = "LINE", _("По строкам")
        ORDER = "ORDER", _("Итог заказа одной формулой")

    # Режим округления: «по строкам» — каждая строка вверх до сома (как было);
    # «итог заказа» — вверх до сома округляется сумма заказа, а разница
    # раскладывается по строкам так, чтобы итог оставался суммой строк.
    rounding_mode = models.CharField(
        _("округление"), max_length=10, choices=Rounding.choices, default=Rounding.LINE,
    )
    # Границы здравого смысла (2026-10-10, CALC-06 / XL-08). Строка дороже
    # порога — касса спрашивает подтверждение (сантиметры вместо метров дают
    # чек на миллиард). Потолок складовщика — жёсткий отказ; 0 — потолка нет.
    confirm_line_total = models.DecimalField(
        _("порог подтверждения суммы строки"), max_digits=14, decimal_places=2,
        default=Decimal("100000"),
        help_text=_("Строка дороже — касса просит подтвердить. 0 — не спрашивать"),
    )
    staff_line_cap = models.DecimalField(
        _("потолок суммы строки для складовщика"), max_digits=14, decimal_places=2,
        default=Decimal("0"),
        help_text=_("Складовщик не оформит строку дороже. 0 — без потолка"),
    )
    # Нижняя граница ручной цены работы складовщика, % от каталога (CALC-08).
    staff_min_price_percent = models.DecimalField(
        _("нижняя граница ручной цены работы, %"), max_digits=5, decimal_places=2,
        default=Decimal("0"),
        help_text=_("Складовщик не впишет цену работы ниже этой доли каталога. 0 — без границы"),
    )
    # Предупреждение о старом долге в кассе (CLI-03). 0 — не предупреждать.
    debt_warn_days = models.PositiveSmallIntegerField(
        _("предупреждать о долге старше, дней"), default=0,
        help_text=_("0 — не предупреждать"),
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

    def clean(self):
        # STAFF-05 (волна 2): доля мастера — процент от работы, 0–100 (django-admin).
        from django.core.exceptions import ValidationError

        super().clean()
        pct = self.master_commission_percent
        if pct is not None and not (Decimal("0") <= pct <= Decimal("100")):
            raise ValidationError({"master_commission_percent": "Процент от 0 до 100."})

    def __str__(self) -> str:
        return f"ЗП мастера {self.master_commission_percent}%"


class ThicknessCoefficient(models.Model):
    """Коэффициент работы по толщине материала (2026-10-10, CALC-02).

    «Вид услуги × толщина → коэффициент», как таблица ВПР с приближённым
    совпадением в Excel владельца: строка действует для толщин от
    `thickness_from` мм и выше, пока её не перебьёт строка с большей границей.
    Ставка работы умножается на коэффициент. Нет строки — коэффициент 1.
    """

    kind = models.CharField(_("вид услуги"), max_length=20, choices=PrintingService.Kind.choices)
    thickness_from = models.DecimalField(_("толщина от, мм"), max_digits=6, decimal_places=2)
    coefficient = models.DecimalField(_("коэффициент"), max_digits=6, decimal_places=3)

    class Meta:
        verbose_name = _("коэффициент по толщине")
        verbose_name_plural = _("коэффициенты по толщине")
        ordering = ["kind", "thickness_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "thickness_from"], name="thickness_coef_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} от {self.thickness_from} мм × {self.coefficient}"


class RateMatrixEntry(models.Model):
    """Ставка услуги для материала или толщины (2026-10-10, CALC-05 / F8).

    Матрица «услуга (она же станок) × материал или толщина → ставка». Приоритет
    при продаже: матрица по материалу → матрица по толщине → прежняя цепочка
    (ставка станка, затем ставка материала). Ставка — за пог.м реза у резки и
    за кв.м у прочих площадных услуг. Ровно одно из полей `material` и
    `thickness_from` заполнено.
    """

    service = models.ForeignKey(
        PrintingService, on_delete=models.CASCADE, related_name="rate_matrix",
    )
    material = models.ForeignKey(
        "warehouse.Material", on_delete=models.CASCADE, null=True, blank=True,
        related_name="rate_matrix",
    )
    thickness_from = models.DecimalField(
        _("толщина от, мм"), max_digits=6, decimal_places=2, null=True, blank=True,
    )
    rate = models.DecimalField(_("ставка"), max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = _("ставка матрицы")
        verbose_name_plural = _("матрица ставок")
        ordering = ["service", "thickness_from", "material"]
        constraints = [
            models.UniqueConstraint(
                fields=["service", "material"], condition=models.Q(material__isnull=False),
                name="rate_matrix_unique_material",
            ),
            models.UniqueConstraint(
                fields=["service", "thickness_from"], condition=models.Q(thickness_from__isnull=False),
                name="rate_matrix_unique_thickness",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(material__isnull=False, thickness_from__isnull=True)
                    | models.Q(material__isnull=True, thickness_from__isnull=False)
                ),
                name="rate_matrix_one_key",
            ),
        ]

    def __str__(self) -> str:
        key = self.material.name if self.material_id else f"от {self.thickness_from} мм"
        return f"{self.service.name} / {key} = {self.rate}"
