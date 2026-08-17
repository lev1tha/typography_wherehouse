from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


class MaterialType(models.Model):
    """Тип материала: Форекс, Акрил, Оргстекло, Алюкобонд, Ромарк.

    Раньше типа не было вовсе: он был зашит в название («форекс 8мм»), а отчёт
    по резке угадывал его подстрокой. На реальной номенклатуре угадывание
    ошибалось — «синий бишкек», «день ночь» и «салатовый» это акрил, но в
    отчёте они падали в «Прочее».

    Справочник, а не перечисление в коде: новый тип админ заводит сам, как
    вид расхода в финотчёте.
    """

    code = models.SlugField(
        _("код"), max_length=40, unique=True, allow_unicode=True,
        help_text=_("Внутренний ключ. У встроенных постоянный, у своих — из названия."),
    )
    name = models.CharField(_("название"), max_length=80)
    # Встроенный тип нельзя удалить: на нём висит каталог и разбивка отчётов.
    is_builtin = models.BooleanField(_("встроенный"), default=False)
    position = models.PositiveIntegerField(_("порядок"), default=100)
    is_archived = models.BooleanField(_("скрыт"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("тип материала")
        verbose_name_plural = _("типы материалов")
        ordering = ["position", "name"]

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def make_code(name: str) -> str:
        base = slugify(name or "", allow_unicode=True)[:32] or "tip"
        code, n = base, 2
        while MaterialType.objects.filter(code=code).exists():
            code = f"{base}-{n}"
            n += 1
        return code


class ProductionSite(models.Model):
    """Откуда возят материал: Бишкек, Глобал. Колонка «производство» в его листе."""

    code = models.SlugField(_("код"), max_length=40, unique=True, allow_unicode=True)
    name = models.CharField(_("название"), max_length=80)
    is_builtin = models.BooleanField(_("встроенное"), default=False)
    position = models.PositiveIntegerField(_("порядок"), default=100)
    is_archived = models.BooleanField(_("скрыто"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("производство")
        verbose_name_plural = _("производства")
        ordering = ["position", "name"]

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def make_code(name: str) -> str:
        base = slugify(name or "", allow_unicode=True)[:32] or "proizvodstvo"
        code, n = base, 2
        while ProductionSite.objects.filter(code=code).exists():
            code = f"{base}-{n}"
            n += 1
        return code


class Material(models.Model):
    """Raw material stocked in the warehouse (paper, ink, cardboard, ...).

    `name` is registered for translation in translation.py so the catalogue can
    be served in RU / KY / EN.

    Roll materials (``is_roll_material=True``) are received as rolls (lots) and
    sold by area (кв.м). Their ``quantity`` is the sum of remaining roll areas,
    ``price_per_unit`` is the retail price per кв.м, and ``purchase_price`` the
    cost per кв.м. See the ``Roll`` model and warehouse.rolls.

    Свойства материала — тип, толщина, цвет, артикул, размер листа — лежат в
    отдельных полях, а не в названии. Заказчик писал их строкой («Орг стекло
    2мм 180*121см», «ЖЕЛТЫЙ лимон 2,5ММ 237»), поэтому ни отфильтровать по
    толщине, ни вывести площадь листа из размера было нельзя.
    """

    class Unit(models.TextChoices):
        SQM = "SQM", _("кв.м")
        METER = "METER", _("пог.м")
        PIECE = "PIECE", _("шт")
        KG = "KG", _("кг")
        LITER = "LITER", _("л")

    class IntakeForm(models.TextChoices):
        """В каком виде материал ПРИХОДИТ: листами или рулоном.

        Раньше это выяснялось только в момент поступления: на материале стояла
        одна галочка «листовой / рулонный», и складовщик каждый раз заново
        выбирал форму — хотя акрил всегда приходит листами, а плёнка всегда
        рулоном. Теперь форма задаётся на материале и подставляется в приход.
        """

        SHEET = "SHEET", _("Лист")
        ROLL = "ROLL", _("Рулон")

    name = models.CharField(_("название"), max_length=255)
    # --- разобранная номенклатура -------------------------------------------
    type = models.ForeignKey(
        MaterialType, on_delete=models.PROTECT, null=True, blank=True,
        related_name="materials", verbose_name=_("тип"),
    )
    thickness_mm = models.DecimalField(
        _("толщина, мм"), max_digits=6, decimal_places=2, null=True, blank=True,
    )
    color = models.CharField(_("цвет"), max_length=80, blank=True, db_index=True)
    article = models.CharField(
        _("артикул"), max_length=40, blank=True,
        help_text=_("Код цвета поставщика, напр. 237"),
    )
    # Размер листа в метрах. Из него считается площадь листа — раньше её
    # вводили руками, хотя она выводится из размера.
    sheet_width = models.DecimalField(
        _("ширина листа, м"), max_digits=6, decimal_places=3, null=True, blank=True,
    )
    sheet_height = models.DecimalField(
        _("высота листа, м"), max_digits=6, decimal_places=3, null=True, blank=True,
    )
    unit = models.CharField(
        _("единица измерения"), max_length=10, choices=Unit.choices, default=Unit.PIECE
    )
    is_roll_material = models.BooleanField(
        _("рулонный материал"),
        default=False,
        help_text=_("Приходит рулонами, продаётся по кв.м, списывается из партий"),
    )
    intake_form = models.CharField(
        _("форма поступления"),
        max_length=10,
        choices=IntakeForm.choices,
        default=IntakeForm.SHEET,
        blank=True,
        help_text=_("Лист или рулон. Только для материалов по кв.м"),
    )
    quantity = models.DecimalField(
        _("остаток"), max_digits=14, decimal_places=4, default=Decimal("0")
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
        _("площадь листа, кв.м"),
        max_digits=12,
        decimal_places=4,
        default=Decimal("0"),
        help_text=_("Считается из размера листа; вводится вручную только без размера"),
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
    # Откуда возят материал — колонка «производство» в складской таблице
    # заказчика (Бишкек, Глобал). Справочник, а не свободный текст: печатать
    # его на каждом материале руками — лишняя работа, а опечатка заводила бы
    # ещё одно «производство», которое потом не сгруппируется.
    production = models.ForeignKey(
        "ProductionSite", on_delete=models.PROTECT, null=True, blank=True,
        related_name="materials", verbose_name=_("производство"),
    )
    # Материал, который больше не продаём. Удалить его нельзя, если по нему были
    # продажи или поступления (иначе поедут суммы в старых чеках и отчётах),
    # поэтому прячем из каталога и кассы, а историю оставляем целой.
    is_archived = models.BooleanField(_("скрыт из каталога"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("материал")
        verbose_name_plural = _("материалы")
        ordering = ["name"]

    def save(self, *args, **kwargs):
        # Площадь листа выводится из его размера. Раньше её вводили руками,
        # хотя размер известен: лишнее поле в форме и лишний повод ошибиться.
        if self.sheet_width and self.sheet_height:
            self.piece_area = (self.sheet_width * self.sheet_height).quantize(
                Decimal("0.0001")
            )
        if not (self.name or "").strip():
            self.name = self.suggested_name()
        super().save(*args, **kwargs)

    def suggested_name(self) -> str:
        """Название из полей: «Акрил белый 2,5 мм 237 · 1.22×2.44».

        Подставляется в форму, но остаётся редактируемым: у заказчика свои
        привычные подписи («синий бишкек»), и отнимать их нельзя.
        """
        def trim(value):
            text = f"{value:f}".rstrip("0").rstrip(".")
            return text.replace(".", ",")

        parts = [self.type.name if self.type_id else "", self.color]
        if self.thickness_mm:
            parts.append(f"{trim(self.thickness_mm)} мм")
        parts.append(self.article)
        if self.sheet_width and self.sheet_height:
            parts.append(f"{trim(self.sheet_width)}×{trim(self.sheet_height)}")
        return " ".join(p for p in parts if p).strip()

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
        """Стоимость того, что лежит на складе, по ЗАКУПОЧНЫМ ценам.

        У материала по кв.м остаток лежит в партиях, и у каждой партии своя
        себестоимость. Раньше здесь стояло ``quantity × purchase_price``, а
        ``purchase_price`` — это цена ПОСЛЕДНЕГО прихода: подорожал акрил вдвое,
        и вдвое дорожал весь остаток, закупленный по старой цене. «Стоимость
        склада» в обзоре и остаток на конец в финотчёте прыгали от одной
        поставки.

        Считаем по партиям, самые старые первыми — они и уйдут следующими
        (FIFO). Остаток сверх партий (инвентаризация правит количество, партий
        не создавая) оцениваем последней закупочной ценой: другой у него нет.
        """
        left = self.quantity or Decimal("0")
        if left <= 0:
            return Decimal("0")
        value = Decimal("0")
        if self.is_roll_material:
            for roll in sorted(self.rolls.all(), key=lambda r: r.received_at):
                if left <= 0:
                    break
                take = min(roll.remaining_area, left)
                if take <= 0:
                    continue
                value += take * roll.cost_per_sqm
                left -= take
        value += left * (self.purchase_price or Decimal("0"))
        return value.quantize(Decimal("0.01"))

    def __str__(self) -> str:
        return self.name


class MaterialMonthOpening(models.Model):
    """Остаток материала на начало месяца — вводится РУКАМИ, как в Excel заказчика.

    Заказчик ведёт склад листами и каждый месяц переносит остаток с прошлого
    месяца сам. Считать его откатом от текущего остатка нельзя: это требует,
    чтобы каждое движение склада за всю историю было записано без единой дыры,
    а на практике цифры разъезжаются и в таблице появляются отрицательные
    остатки. Дальше всё считается по формуле листа:
    ``начало + поступление − проданные = конец``.
    """

    material = models.ForeignKey(
        Material, on_delete=models.CASCADE, related_name="month_openings"
    )
    year = models.PositiveSmallIntegerField(_("год"))
    month = models.PositiveSmallIntegerField(_("месяц"))
    # В той же единице, в которой материал считают в таблице: листы, если у
    # материала задана площадь листа, иначе его собственная единица.
    quantity = models.DecimalField(
        _("остаток на начало месяца"), max_digits=12, decimal_places=2, default=Decimal("0")
    )
    updated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="month_openings",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("остаток на начало месяца")
        verbose_name_plural = _("остатки на начало месяца")
        constraints = [
            models.UniqueConstraint(
                fields=["material", "year", "month"], name="unique_material_month_opening"
            )
        ]
        ordering = ["-year", "-month"]

    def __str__(self) -> str:
        return f"{self.material.name} {self.month:02d}.{self.year}: {self.quantity}"


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
    """Журнал движений склада — единственный ответ на «куда делся материал».

    Пишется КАЖДОЕ изменение остатка, включая продажи. Раньше продажи в журнал
    не попадали вовсе, и он не сходился сам с собой: возврат штучного материала
    записывался приходом, а расхода, который он отменяет, в журнале не было.
    """

    class Type(models.TextChoices):
        SUPPLY = "SUPPLY", _("Поступление")
        SALE = "SALE", _("Продажа")
        RETURN = "RETURN", _("Возврат от клиента")
        ADJUSTMENT = "ADJUSTMENT", _("Корректировка/Инвентаризация")
        WRITE_OFF = "WRITE_OFF", _("Списание (порча/брак/утеря)")

    type = models.CharField(max_length=20, choices=Type.choices)
    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="inventory_logs"
    )
    quantity_changed = models.DecimalField(
        _("изменение количества"),
        max_digits=14,
        decimal_places=4,
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
    # Чек, из-за которого материал ушёл со склада (или вернулся). Ссылка, а не
    # номер текстом: из журнала видно не только «продажа», но и какой заказ, и
    # при возврате мы находим парный расход по тому же чеку.
    receipt = models.ForeignKey(
        "sales.Receipt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_logs",
        verbose_name=_("чек"),
    )
    # Приходная накладная, по которой материал пришёл. Ссылка, а не номер
    # текстом: из журнала видно не только «поступление», но и по какой бумаге —
    # и обратно, из накладной, весь её след на складе.
    supply = models.ForeignKey(
        "Supply",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_logs",
        verbose_name=_("накладная"),
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_logs",
    )
    # Дата САМОЙ операции, а не момента ввода: заказчик вносит поставки задним
    # числом, когда доходят руки — в его Excel даты идут вразнобой (01, 10, 14,
    # 19, 05, 06 июля). При auto_now_add июльская поставка, введённая в августе,
    # уезжала в август и складской лист расходился с его таблицей.
    happened_at = models.DateTimeField(_("дата операции"), default=timezone.now)

    class Meta:
        verbose_name = _("складская операция")
        verbose_name_plural = _("складские операции")
        ordering = ["-happened_at"]

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
        _("площадь при поступлении, кв.м"), max_digits=14, decimal_places=4
    )
    remaining_area = models.DecimalField(
        _("остаток, кв.м"), max_digits=14, decimal_places=4
    )
    purchase_cost = models.DecimalField(
        _("себестоимость рулона"), max_digits=12, decimal_places=2,
        help_text=_("Полная стоимость закупки рулона"),
    )
    # Дата поступления партии — редактируемая по той же причине, что и у
    # складской операции. По ней же идёт FIFO, поэтому партия, внесённая задним
    # числом, встаёт в очередь на списание по своей настоящей дате.
    received_at = models.DateTimeField(_("дата поступления"), default=timezone.now)
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
    def is_depleted(self) -> bool:
        return self.remaining_area <= 0

    @property
    def dimensions_label(self) -> str:
        # Числа без хвоста нулей: «Лист 1.22×2.44 ×5», а не «×5.00».
        def n(v):
            return "?" if v is None else format(Decimal(v).normalize(), "f")

        if self.form == self.Form.SHEET:
            dims = f"{n(self.width)}×{n(self.height)}"
            return f"Лист {dims}" + (f" ×{n(self.sheet_count)}" if self.sheet_count else "")
        return f"Рулон {n(self.width)}×{n(self.length)}м"

    def __str__(self) -> str:
        label = self.code or f"Партия #{self.pk}"
        return f"{label} — {self.material.name}: {self.remaining_area}/{self.initial_area} кв.м"


class Supplier(models.Model):
    """У кого закупаем материал.

    Поставщика в системе не было вовсе: «долг материала» в финотчёте был одной
    суммой, вписанной руками, и на вопрос «сколько я должен Глобалу, а сколько
    бишкекским» ответить было нечем. Справочник, а не свободный текст, — иначе
    опечатка заводит второго «Глобала», и долги разъезжаются по двум карточкам.
    """

    name = models.CharField(_("название"), max_length=160, unique=True)
    phone = models.CharField(_("телефон"), max_length=64, blank=True)
    inn = models.CharField(_("ИНН"), max_length=32, blank=True)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    is_archived = models.BooleanField(_("скрыт"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("поставщик")
        verbose_name_plural = _("поставщики")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Supply(models.Model):
    """Приходная накладная — одна поставка целиком, со строками.

    Раньше приход вводился ПО ОДНОЙ ПОЗИЦИИ, с кнопки на строке материала:
    поставка на восемь позиций — восемь отдельных операций, каждая со своей
    датой. Сверить итог с бумажной накладной было нечем — общей суммы поставки
    система не знала и не могла узнать.

    Документ ПРОВОДИТСЯ сразу при создании: строки уходят на склад теми же
    примитивами, что и раньше (``receive_lot`` для площадных, ``apply_stock_change``
    для штучных), поэтому закуп в финотчёте и складской журнал работают без
    единой правки — они и так считаются по движениям.
    """

    number = models.CharField(
        _("номер накладной"), max_length=64, blank=True,
        help_text=_("Номер бумажной накладной поставщика"),
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, null=True, blank=True,
        related_name="supplies", verbose_name=_("поставщик"),
    )
    # Дата накладной, а не момента ввода: поставки вносят задним числом, и по
    # этой дате идут и FIFO, и закуп месяца.
    received_on = models.DateField(_("дата накладной"), default=timezone.localdate)
    # Сумма, написанная НА БУМАГЕ. Своей суммы документа не заменяет — наоборот,
    # существует ради расхождения с ней: сошлось или нет.
    stated_total = models.DecimalField(
        _("сумма по накладной"), max_digits=14, decimal_places=2,
        null=True, blank=True,
        help_text=_("Как в бумаге. Пусто — сверять не с чем"),
    )
    paid_amount = models.DecimalField(
        _("оплачено поставщику"), max_digits=14, decimal_places=2, default=Decimal("0")
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplies",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("приходная накладная")
        verbose_name_plural = _("приходные накладные")
        ordering = ["-received_on", "-created_at"]

    def __str__(self) -> str:
        label = f"№{self.number}" if self.number else f"#{self.pk}"
        return f"Накладная {label} от {self.received_on}"

    @property
    def total_cost(self) -> Decimal:
        """Сумма строк — то, что система реально приняла на склад."""
        return sum((line.cost for line in self.lines.all()), Decimal("0"))

    @property
    def discrepancy(self) -> Decimal:
        """Бумага минус система. Не ноль — где-то опечатка, и её видно сразу."""
        if self.stated_total is None:
            return Decimal("0")
        return self.stated_total - self.total_cost

    @property
    def debt(self) -> Decimal:
        """Сколько мы ещё должны поставщику по этой накладной."""
        owed = self.total_cost - self.paid_amount
        return owed if owed > 0 else Decimal("0")


class SupplyLine(models.Model):
    """Строка приходной накладной: что и почём приняли.

    Хранится отдельно от самой партии (``Roll``), потому что штучный материал
    партий не заводит вовсе, а строка нужна обоим: по ней документ печатается,
    сверяется и, если понадобится, отменяется.
    """

    class Form(models.TextChoices):
        SHEET = "SHEET", _("Лист")
        ROLL = "ROLL", _("Рулон")
        QTY = "QTY", _("По количеству")

    supply = models.ForeignKey(Supply, on_delete=models.CASCADE, related_name="lines")
    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="supply_lines"
    )
    form = models.CharField(max_length=10, choices=Form.choices, default=Form.SHEET)
    width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    height = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    length = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sheet_count = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Сколько встало на склад в единицах материала (кв.м или штуки). Считается
    # при проведении и хранится, чтобы отмену не пришлось выводить заново.
    quantity = models.DecimalField(
        _("принято"), max_digits=14, decimal_places=4, default=Decimal("0")
    )
    cost = models.DecimalField(_("сумма строки"), max_digits=12, decimal_places=2)
    code = models.CharField(_("маркировка партии"), max_length=120, blank=True)
    # Созданная партия — у площадных материалов. У штучных партий нет.
    roll = models.OneToOneField(
        Roll, on_delete=models.SET_NULL, null=True, blank=True, related_name="supply_line"
    )

    class Meta:
        verbose_name = _("строка накладной")
        verbose_name_plural = _("строки накладной")
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.material.name}: {self.quantity} на {self.cost}"

    @property
    def unit_cost(self) -> Decimal:
        """Себестоимость за единицу — та цифра, по которой заказчик сверяет
        «подорожало или нет»."""
        if not self.quantity:
            return Decimal("0")
        return (self.cost / self.quantity).quantize(Decimal("0.01"))
