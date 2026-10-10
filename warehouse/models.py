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
    # --- Рулон: ширина и цена за ПОГОННЫЙ метр --------------------------
    #
    # Рулон продаётся длиной, а не площадью: ширина у него не выбор клиента, а
    # свойство товара. Ткань 0.9 м режут поперёк на всю ширину, и 40 см ширины
    # купить нельзя — отрежут всё равно всю. Поэтому ширина живёт ЗДЕСЬ, а не
    # полем ввода в кассе: любое поле, которое можно поменять, рано или поздно
    # поменяют, и в чек уезжало «1.5 × 1.4 = 2.1 кв.м» вместо 1.4 пог.м.
    roll_width = models.DecimalField(
        _("ширина рулона, м"), max_digits=8, decimal_places=3,
        null=True, blank=True,
        help_text=_("Подставляется в приёмку. Фактическая ширина замораживается в партии"),
    )
    # Цена продажи за погонный метр. Считать её через цену за кв.м нельзя:
    # владелец держит прайс в метрах («туника 300 сом/м»), и деление 300 ÷ 0.9 =
    # 333.33 он в уме делать не станет, а округлит до 330 — и подарит по 3 сома
    # с каждого метра рулона.
    price_per_pm = models.DecimalField(
        _("цена продажи, сом/пог.м"), max_digits=12, decimal_places=2,
        default=Decimal("0"),
    )
    cut_rate_per_pm = models.DecimalField(
        _("ставка резки, сом/пог.м"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Стоимость работы резки за погонный метр для этого материала"),
    )
    # Наценка от закупа (STK-03, волна 2). Цена продажи по-прежнему вводится
    # руками — это ПОДСКАЗКА: закуп последней партии × (1 + наценка). Пусто —
    # подсказки нет, всё как раньше. См. warehouse/pricing.py.
    markup_percent = models.DecimalField(
        _("наценка, %"), max_digits=7, decimal_places=2, null=True, blank=True,
        help_text=_("Цена-подсказка = закуп последней партии × (1 + наценка)"),
    )
    # Коэффициент использования материала при раскрое (STK-07/G4-N1, волна 2).
    # Детали 0.77 кв.м из листа при КИМ 62 % съедают 1.24 кв.м: остальное —
    # обрезки, которые никуда не продать. Пусто — списываем ровно площадь
    # деталей, как раньше. См. `warehouse.rolls.kim_factor`.
    kim_percent = models.DecimalField(
        _("КИМ раскроя, %"), max_digits=5, decimal_places=2, null=True, blank=True,
        help_text=_("Списание куска = площадь деталей ÷ КИМ; целые листы — как есть"),
    )
    # Минимальный остаток В ЕДИНИЦАХ МАТЕРИАЛА (STK-06, волна 2): листы у листа,
    # метры у рулона, штуки у штучного. «5 листов» владелец пересчитывал в
    # 14,88 кв.м сам. Задан — из него считается `critical_balance` (кв.м), на
    # котором держатся бейдж «на исходе» и оповещение; пусто — порог как был.
    min_stock = models.DecimalField(
        _("минимальный остаток, в единицах материала"), max_digits=12, decimal_places=2,
        null=True, blank=True,
        help_text=_("Листы, метры или штуки. «К заказу» — до двух минимумов"),
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
        if self.min_stock is not None:
            # Порог в единицах материала → порог хранения (кв.м у листа и рулона).
            from .reorder import to_stock_units

            self.critical_balance = to_stock_units(self, self.min_stock)
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
        an admin has set a wholesale price). Otherwise the regular piece price.

        Ступени опта (CLI-02, волна 2): «от 10 листов — 3 500, от 25 — 3 200».
        Берётся ступень с самым большим порогом, которого достигло количество;
        прежняя пара «опт / опт от» — ещё одна ступень наравне с ними. Ступеней
        нет — всё как раньше.
        """
        qty = Decimal(str(qty or 0))
        steps = [(t.min_qty, t.price) for t in self.price_tiers.all() if t.price and t.min_qty]
        if self.wholesale_price and self.wholesale_min_qty:
            steps.append((self.wholesale_min_qty, self.wholesale_price))
        reached = [s for s in steps if qty >= s[0]]
        if reached:
            return max(reached, key=lambda s: s[0])[1]
        return self.piece_price

    @property
    def sheets_remaining(self):
        """Stock expressed in whole sheets (кв.м ÷ площадь листа). None when the
        material isn't measured in sheets (no piece_area set)."""
        if self.piece_area and self.piece_area > 0:
            return (self.quantity / self.piece_area).quantize(Decimal("0.01"))
        return None

    @property
    def sells_by_metre(self) -> bool:
        """Продаётся ли материал погонными метрами.

        Рулон — да, лист — нет. Развилка по СПРАВОЧНИКУ, а не по выбору кассира:
        способ расчёта определяется товаром, который он уже выбрал.

        Решает ФОРМА, и только она. Раньше сюда входила ещё и ширина из карточки
        — и рулон с незаполненной шириной молча возвращался к четырём вкладкам и
        продаже по площади: та самая ошибка «1.5 × 1.4 = 2.1 кв.м вместо 1.4
        пог.м», от которой уходили, только спрятанная за пустым полем. Ширина
        обязательна у формы «Рулон» (проверяет сериализатор), а продажа метрами
        от неё не зависит: метры считаются по ширине КАЖДОЙ партии.
        """
        return bool(
            self.is_roll_material and self.intake_form == self.IntakeForm.ROLL
        )

    @property
    def sells_roll_by_area(self) -> bool:
        """Рулон, который можно продать ещё и по площади ИЗДЕЛИЯ (CALC-10, D-140).

        Вторая цена рулона — `price_per_sqm` («баннер 220 сом/кв.м»): клиент
        платит ширина × длина изделия × цена, а со склада уходит вся ширина
        рулона × длина. Цена не задана — рулон продаётся только метрами, как
        раньше. Запасной цены здесь нет (`sqm_price` подставил бы
        `price_per_unit`): способ продажи включает только явно заданная цена.
        """
        return bool(self.sells_by_metre and self.price_per_sqm and self.price_per_sqm > 0)

    @property
    def metres_remaining(self):
        """Остаток в погонных метрах — суммой ПО РУЛОНАМ, у каждого своя ширина.

        Делить общий остаток на ширину из карточки нельзя по двум причинам.
        Первая: правка опечатки «0.9 → 1.0» в справочнике молча пересчитала бы
        остатки всех рулонов, включая давно закрытые. Вторая: под одной
        карточкой законно лежит оракал 1.0, 1.26 и 1.52 — общей ширины у него
        просто нет.

        Ширина заморожена в партии при приёмке, метры считаются от неё.
        """
        rolls = [r for r in self.rolls.all() if r.width and r.remaining_area > 0]
        if not rolls:
            return None
        total = sum((r.remaining_area / r.width for r in rolls), Decimal("0"))
        return total.quantize(Decimal("0.01"))

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

        ШТУЧНЫЙ материал — тем же циклом: с 27.08 у него тоже партии
        (`Roll.Form.PIECE`), и приход прибавляет штуки к `quantity`. Раньше цикл
        стоял под `is_roll_material`, и штучный оценивался ценой последнего
        прихода — ровно та ошибка, от которой партии и заводились. Штучный без
        партий уходит в «хвост» и считается по карточке, как раньше.

        Это ЕДИНСТВЕННАЯ формула стоимости склада: ей же считают и «Обзор», и
        «Склад (оборот)» в «Финансах» (`stock_value_total`). Своя формула в
        финотчёте считала штучные партии дважды — партией и ещё раз
        количеством × закупочную — и на проде расходилась с «Обзором» на
        двести с лишним тысяч (15.09).
        """
        left = self.quantity or Decimal("0")
        if left <= 0:
            return Decimal("0")
        value = Decimal("0")
        for roll in sorted(self.rolls.all(), key=lambda r: r.received_at):
            if left <= 0:
                break
            take = min(roll.remaining_area, left)
            if take <= 0:
                continue
            value += roll.cost_of(take)
            left -= take
        value += left * (self.purchase_price or Decimal("0"))
        return value.quantize(Decimal("0.01"))

    def __str__(self) -> str:
        return self.name


def stock_value_total(upto=None) -> Decimal:
    """Стоимость всего склада — сумма `Material.stock_value`.

    Одна функция на «Обзор» и «Финансы»: две формулы одной цифры уже
    разъехались (штучные партии в финотчёте считались дважды).

    Скрытый материал с остатком СЮДА ВХОДИТ — он физически на полке (решение
    18.08, см. `audit/views.py`). Отбор по `quantity > 0` это и даёт: пустой
    материал, скрытый или нет, стоит ноль и так.

    ``upto`` — день, НА КОНЕЦ которого считаем. Без него — «сейчас».

    Зачем отматывать назад. Отчёт за месяц показывал СЕГОДНЯШНИЙ склад, какой
    бы месяц ни выбрали: открываешь август, где ни одной продажи и ни одного
    прихода, — выручка 0, закуп 0, а склад 1 184 614. Цифра из другого времени
    стояла среди месячных и читалась как месячная.

    Как считаем. Остаток на дату — это СУММА ДВИЖЕНИЙ ЖУРНАЛА по этот день
    включительно, а не сегодняшний остаток минус движения после. Разница видна
    там, где остаток журналом не объясняется: на проде такой хвост 0.084 кв.м,
    и при счёте «назад от сегодня» он протягивался в любой прошлый месяц —
    август показывал 48 сомов вместо нуля. Журнал же начинается 1 сентября, и
    до него склада не было вовсе.

    Оцениваем по партиям, пришедшим не позже этой даты, и с самых СВЕЖИХ:
    старые уходят первыми (FIFO), значит на полке остаются последние.

    Это РЕКОНСТРУКЦИЯ, а не снимок: система не хранит, сколько оставалось в
    каждой партии на каждый день. Порядок списания мог быть не строго FIFO
    (инвентаризация и отход умеют брать конкретную партию), поэтому цифра
    прошлого месяца — близкая, но не до копейки. «Сейчас» считается точно, по
    остаткам самих партий.
    """
    if upto is None or upto >= timezone.localdate():
        return sum(
            (m.stock_value for m in Material.objects.filter(quantity__gt=0).prefetch_related("rolls")),
            Decimal("0"),
        )

    # Снимок на эту дату (STK-04, волна 2) — замороженная цифра: снят при
    # закрытии месяца или командой, от поставок задним числом не плывёт.
    snapshot = StockSnapshot.objects.filter(as_of=upto).values_list("value", flat=True).first()
    if snapshot is not None:
        return snapshot

    moved = {
        row["material"]: row["v"] or Decimal("0")
        for row in InventoryLog.objects.filter(happened_at__date__lte=upto)
        .values("material")
        .annotate(v=models.Sum("quantity_changed"))
    }
    total = Decimal("0")
    for material in Material.objects.prefetch_related("rolls"):
        left = moved.get(material.id, Decimal("0"))
        if left <= 0:
            continue
        lots = [
            roll for roll in material.rolls.all()
            if timezone.localtime(roll.received_at).date() <= upto
        ]
        for roll in sorted(lots, key=lambda r: r.received_at, reverse=True):
            if left <= 0:
                break
            take = min(roll.initial_area, left)
            total += roll.cost_of(take)
            left -= take
        # Остаток сверх партий — по последней закупочной, как и «сейчас».
        total += left * (material.purchase_price or Decimal("0"))
    return total.quantize(Decimal("0.01"))


class MaterialPriceTier(models.Model):
    """Ступень опта за лист (CLI-02, волна 2): «от N листов — цена»."""

    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name="price_tiers")
    min_qty = models.DecimalField(_("от, листов"), max_digits=12, decimal_places=2)
    price = models.DecimalField(_("цена за лист"), max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = _("ступень опта")
        verbose_name_plural = _("ступени опта")
        ordering = ["material", "min_qty"]
        constraints = [
            models.UniqueConstraint(fields=["material", "min_qty"], name="price_tier_material_min_qty"),
        ]

    def __str__(self) -> str:
        return f"{self.material_id}: от {self.min_qty} — {self.price}"


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


class RollStocktake(models.Model):
    """Акт промера рулона: что показывала система, что намерили рулеткой.

    Инвентаризация правкой остатка не годится: она приводит число к факту и на
    этом заканчивается — расхождение исчезает вместе с причиной, и через месяц
    на вопрос «куда делись полтора метра» ответить нечем. Учёт без этого
    остаётся гипотезой.

    Поэтому расхождение хранится ЧИСЛОМ и отдельной записью: акт не
    пересчитывается из остатков и не меняется задним числом. Остаток рулона
    правится в ту же операцию, но объяснение остаётся навсегда.
    """

    class Reason(models.TextChoices):
        SUPPLIER = "SUPPLIER", _("Недомер при приёмке")
        CUTTING = "CUTTING", _("Потери при резке")
        DAMAGE = "DAMAGE", _("Порча")
        MISCOUNT = "MISCOUNT", _("Ошибка учёта")
        OTHER = "OTHER", _("Прочее")

    # Строкой, а не классом: акт объявлен выше самой модели Roll.
    roll = models.ForeignKey(
        "Roll", on_delete=models.PROTECT, related_name="stocktakes",
        verbose_name=_("рулон"),
    )
    # Обе цифры — в ПОГОННЫХ метрах: рулон меряют рулеткой, а не в квадратах.
    expected_metres = models.DecimalField(
        _("было по системе, м"), max_digits=12, decimal_places=2
    )
    counted_metres = models.DecimalField(
        _("намерено по факту, м"), max_digits=12, decimal_places=2
    )
    # Хранится, а не вычисляется: остаток рулона потом изменится продажами, и
    # разница «намерено − было» перестала бы восстанавливаться.
    difference = models.DecimalField(
        _("расхождение, м"), max_digits=12, decimal_places=2,
        help_text=_("Минус — недостача, плюс — излишек"),
    )
    reason_code = models.CharField(
        _("причина"), max_length=20, choices=Reason.choices, default=Reason.OTHER
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="roll_stocktakes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("акт промера рулона")
        verbose_name_plural = _("акты промера рулонов")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        sign = "+" if self.difference > 0 else ""
        return f"Промер {self.roll_id}: {sign}{self.difference} м"


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
        # Исправление опечатки в принятой партии (2026-10-10, D-70). Сам приход
        # правится на месте — как ячейка в Excel, — а эта запись хранит «было →
        # стало». Движения она не несёт (количество 0, себестоимости нет): ни
        # продажа, ни потеря, ни промер, в ОПиУ не попадает.
        CORRECTION = "CORRECTION", _("Исправление прихода")
        # Перемещение между площадками (STK-05/G4-N4, волна 2). Остаток
        # материала не меняется (количество 0), закупа и потерь нет — только
        # где лежит. Подробности — в `StockTransfer`.
        TRANSFER = "TRANSFER", _("Перемещение")

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
    # Метры — для рулона. Склад считает площадь, и в журнале продажа рулона
    # стояла как «−8 кв.м», хотя в цехе отрезали 5 пог.м: пересчитать одно в
    # другое постфактум нельзя, у каждой партии своя ширина. Поэтому метры
    # пишем В МОМЕНТ операции, когда ширина партии известна точно. Пусто —
    # операция была не в метрах (лист, штучный, правка общего остатка).
    metres_changed = models.DecimalField(
        _("изменение в пог.м"),
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    actual_price = models.DecimalField(
        _("новая цена закупки"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    # Себестоимость ушедшего материала — по партиям, из которых он ушёл. Нужна
    # списанию и отходу: «сколько денег выбросили» иначе не узнать никак —
    # у продажи эта цифра лежит на строке чека, у брака строки чека нет. Пусто
    # у прихода и у старых записей.
    cost = models.DecimalField(
        _("себестоимость движения"), max_digits=14, decimal_places=2,
        null=True, blank=True,
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
    # Строка чека, которая породила это движение. Нужна правке состава: сторно
    # должно задеть записи ИМЕННО этой строки, а не «первую продажу того же
    # материала» — иначе при двух строках одного материала журнал переставал
    # сходиться с остатком. Пусто у старых записей и у движений не из чека.
    receipt_item = models.ForeignKey(
        "sales.TransactionItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_logs",
        verbose_name=_("строка чека"),
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
    # Партия, к которой относится запись: приход и его исправления. Раньше
    # приход партии искали по материалу и площади — две одинаковые поставки
    # не различались, а после исправления количества площадь уже не та.
    # Пусто у старых записей и у движений не по партии.
    roll = models.ForeignKey(
        "Roll",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_logs",
        verbose_name=_("партия"),
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
    """A received lot of a material.

    A lot arrives either as a roll (width × length), a stack of sheets
    (width × height × count) or a plain count of pieces. Either way
    `initial_area` is the source of truth for stock. Each lot keeps its own
    cost, so cost per unit is computed per lot. Sales consume FIFO across lots.

    ШТУЧНАЯ партия (`Form.PIECE`, 2026-08-27, просьба владельца) хранится тем же
    полем: у неё `initial_area` — это КОЛИЧЕСТВО ШТУК, а `cost_per_sqm` —
    себестоимость одной штуки. Заводить рядом второй, почти такой же механизм
    ради другой единицы значило бы удвоить всё, что вокруг партий уже написано:
    FIFO, возврат в ту же партию, себестоимость снимком, журнал. Единица тут и
    так живёт в материале (`Material.unit`), а не в партии.
    """

    class Form(models.TextChoices):
        ROLL = "ROLL", _("Рулон")
        SHEET = "SHEET", _("Лист")
        PIECE = "PIECE", _("Штучный")

    material = models.ForeignKey(
        Material, on_delete=models.PROTECT, related_name="rolls"
    )
    code = models.CharField(_("маркировка партии"), max_length=120, blank=True)
    # Откуда приехала ИМЕННО ЭТА партия. У материала производство тоже есть, но
    # оно про «откуда возим обычно», а партии одного акрила приходят из разных
    # мест — китайская и бишкекская лежат на складе одновременно и стоят
    # по-разному. Раньше это писали словом в маркировку («бишкек»), то есть
    # свободным текстом: ни отфильтровать, ни свести.
    #
    # Пустое значение — законное: старые партии его не знают, а у поставки
    # «со склада» производства может не быть вовсе.
    production = models.ForeignKey(
        "ProductionSite", on_delete=models.PROTECT, null=True, blank=True,
        related_name="rolls", verbose_name=_("производство"),
    )
    # ГДЕ ЛЕЖИТ партия (STK-05, волна 2) — в отличие от `production` («откуда
    # приехала»). Пусто — площадка не указана (старые партии). Часть партии,
    # перевезённая на другую площадку, — `LotPlacement`.
    site = models.ForeignKey(
        "ProductionSite", on_delete=models.PROTECT, null=True, blank=True,
        related_name="stored_rolls", verbose_name=_("площадка хранения"),
    )
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
    # Сколько метров ЗАЯВИЛ поставщик. Принятое по факту лежит в `length`.
    # Без этой пары систематический недолив не виден вообще: рулон за рулоном
    # приходит на метр короче, цех платит за заявленное, а замечает это через
    # год по интуиции, а не по цифре.
    declared_length = models.DecimalField(
        _("заявлено поставщиком, м"), max_digits=10, decimal_places=2,
        null=True, blank=True,
    )
    # Дата поступления партии — редактируемая по той же причине, что и у
    # складской операции. По ней же идёт FIFO, поэтому партия, внесённая задним
    # числом, встаёт в очередь на списание по своей настоящей дате.
    received_at = models.DateTimeField(_("дата поступления"), default=timezone.now)
    # ДОЛГ ПОСТАВЩИКУ за эту партию — сколько ещё не заплатили. Приход «в
    # долг» одиночной кнопкой раньше не записывал его нигде: система знала,
    # что материал пришёл, но не знала, что за него должны, и оплату потом
    # провести было некуда. Ноль — заплатили сразу или способ оплаты не
    # назвали (старые приходы). У партии из накладной долг живёт в самой
    # накладной (`Supply.debt`), здесь ноль.
    supplier_debt = models.DecimalField(
        _("долг поставщику"), max_digits=12, decimal_places=2, default=Decimal("0"),
    )
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
        """Цена кв.м (штуки) партии, до копеек — ДЛЯ ПОКАЗА. Считать по ней
        себестоимость нельзя: см. `cost_of`."""
        if not self.initial_area:
            return Decimal("0")
        return (self.purchase_cost / self.initial_area).quantize(Decimal("0.01"))

    def cost_of(self, area) -> Decimal:
        """Себестоимость `area` кв.м (штук) этой партии — закуп × взято / принято.

        Без промежуточного округления цены кв.м до копеек (STK-10): 20 листов
        партии за 92 280 давали 92 280,20, потому что 92 280 / 59.536 = 1 550.0034
        округлялось до 1 550,00 и умножалось обратно с хвостом. Теперь партия,
        проданная целиком, стоит ровно свой закуп. Не округляет — округляет тот,
        кто записывает сумму.
        """
        if not self.initial_area:
            return Decimal("0")
        return self.purchase_cost * Decimal(area) / self.initial_area

    # --- Рулон в погонных метрах: считаем СВОЕЙ шириной ------------------
    #
    # Ширина партии заморожена при приёмке и живёт здесь, а не в карточке
    # материала. Правка опечатки «0.9 → 1.0» в справочнике не должна задним
    # числом пересчитывать остатки уже принятых рулонов — включая закрытые.
    # Она же позволяет держать под одной карточкой оракал 1.0, 1.26 и 1.52:
    # ширина у каждого рулона своя, а материал один.
    @property
    def metres_initial(self):
        if not self.width:
            return None
        return (self.initial_area / self.width).quantize(Decimal("0.01"))

    @property
    def metres_remaining(self):
        if not self.width:
            return None
        return (self.remaining_area / self.width).quantize(Decimal("0.01"))

    @property
    def cost_per_pm(self) -> Decimal:
        """Себестоимость погонного метра — то, в чём считает поставщик."""
        metres = self.metres_initial
        if not metres:
            return Decimal("0")
        return (self.purchase_cost / metres).quantize(Decimal("0.01"))

    @property
    def shortfall(self):
        """Недолив: заявлено минус принято. None — сверять не с чем."""
        if self.declared_length is None or self.length is None:
            return None
        return self.declared_length - self.length

    @property
    def is_depleted(self) -> bool:
        return self.remaining_area <= 0

    @property
    def dimensions_label(self) -> str:
        # Числа без хвоста нулей: «Лист 1.22×2.44 ×5», а не «×5.00».
        def n(v):
            return "?" if v is None else format(Decimal(v).normalize(), "f")

        if self.form == self.Form.PIECE:
            # У штучной партии размеров нет вовсе — только количество. Единицу
            # берём у материала: это могут быть штуки, килограммы или литры.
            return f"{n(self.initial_area)} {self.material.get_unit_display()}"
        if self.form == self.Form.SHEET:
            # Партию могли принять просто площадью — счёт поставщика был в
            # квадратах, размеров никто не называл. Тогда «Лист ?×?» врёт
            # вопросительными знаками там, где всё известно: площадь и есть
            # то, что приняли.
            if not self.width or not self.height:
                return f"{n(self.initial_area)} кв.м"
            dims = f"{n(self.width)}×{n(self.height)}"
            return f"Лист {dims}" + (f" ×{n(self.sheet_count)}" if self.sheet_count else "")
        return f"Рулон {n(self.width)}×{n(self.length)}м"

    def __str__(self) -> str:
        label = self.code or f"Партия #{self.pk}"
        return f"{label} — {self.material.name}: {self.remaining_area}/{self.initial_area} кв.м"


class LotPlacement(models.Model):
    """Часть остатка партии, лежащая НЕ на площадке партии (STK-05, волна 2).

    Партия не делится: её приход, цена, продажи и возвраты остаются как были
    (на ней держатся накладная, «Исправить приход» и возврат в ту же партию).
    Перемещение лишь записывает, сколько её кв.м (штук) лежит на другой
    площадке. Продажа площадку не знает и уходит сперва с площадки партии;
    перевезённое тает, когда партии на «своей» площадке не осталось
    (`warehouse.sites.placements`).
    """

    roll = models.ForeignKey(Roll, on_delete=models.CASCADE, related_name="placements")
    site = models.ForeignKey(
        "ProductionSite", on_delete=models.PROTECT, null=True, blank=True,
        related_name="lot_placements", verbose_name=_("площадка"),
    )
    area = models.DecimalField(_("кв.м (шт)"), max_digits=14, decimal_places=4)

    class Meta:
        verbose_name = _("часть партии на площадке")
        verbose_name_plural = _("части партий на площадках")
        constraints = [
            models.UniqueConstraint(fields=["roll", "site"], name="lot_placement_roll_site"),
        ]

    def __str__(self) -> str:
        return f"{self.roll_id} @ {self.site_id}: {self.area}"


class StockTransfer(models.Model):
    """Документ «Перемещение» между площадками (STK-05/G4-N4, волна 2).

    Раньше перевоз в Глобал изображали списанием и новым приходом: в ОПиУ —
    потеря, в закупе — покупка, которых не было. Здесь ни закупа, ни потерь:
    остаток материала тот же, меняется только площадка.
    """

    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="transfers")
    from_site = models.ForeignKey(
        "ProductionSite", on_delete=models.PROTECT, null=True, blank=True,
        related_name="transfers_out", verbose_name=_("откуда"),
    )
    to_site = models.ForeignKey(
        "ProductionSite", on_delete=models.PROTECT, null=True, blank=True,
        related_name="transfers_in", verbose_name=_("куда"),
    )
    area = models.DecimalField(_("перемещено, кв.м (шт)"), max_digits=14, decimal_places=4)
    cost = models.DecimalField(_("по закупу"), max_digits=14, decimal_places=2, default=Decimal("0"))
    happened_on = models.DateField(_("дата"), default=timezone.localdate)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_transfers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("перемещение")
        verbose_name_plural = _("перемещения")
        ordering = ["-happened_on", "-id"]

    def __str__(self) -> str:
        return f"{self.material} {self.area}: {self.from_site_id} → {self.to_site_id}"


class StockSnapshot(models.Model):
    """Снимок склада на конец дня (STK-04, волна 2): сколько лежало в каждой
    партии и почём.

    Склад «на прошлую дату» раньше был только реконструкцией по журналу —
    близкой, но не до копейки, и плывущей от каждой поставки, внесённой задним
    числом. Снимок замораживает цифру: снимается при закрытии периода (на дату
    закрытия) и командой `stock_snapshot`; «склад на дату» берёт его, если он
    есть (`stock_value_total`, `warehouse.snapshots.stock_on_date`).
    """

    as_of = models.DateField(_("на конец дня"), unique=True)
    source = models.CharField(_("откуда"), max_length=20, default="command")
    value = models.DecimalField(_("стоимость склада"), max_digits=16, decimal_places=2, default=Decimal("0"))
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_snapshots",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("снимок склада")
        verbose_name_plural = _("снимки склада")
        ordering = ["-as_of"]

    def __str__(self) -> str:
        return f"Склад на {self.as_of}: {self.value}"


class StockSnapshotLine(models.Model):
    """Строка снимка: партия (или «сверх партий», `roll` пуст) материала."""

    snapshot = models.ForeignKey(StockSnapshot, on_delete=models.CASCADE, related_name="lines")
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name="snapshot_lines")
    roll = models.ForeignKey(Roll, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="snapshot_lines")
    quantity = models.DecimalField(_("остаток, кв.м (шт)"), max_digits=14, decimal_places=4)
    value = models.DecimalField(_("по закупу"), max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = _("строка снимка склада")
        verbose_name_plural = _("строки снимка склада")


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
    # Чем платили: ящик или счёт. Пусто — не платили (взяли в долг), и в кассу
    # накладная не пишется. Сумму берём из `paid_amount`: оплата бывает
    # частичной, и остаток честно висит в `debt`.
    paid_account = models.CharField(
        _("чем заплатили"), max_length=10, blank=True,
        choices=[("CASH", _("Наличные")), ("BANK", _("Банк"))],
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    # НАЧАЛЬНЫЕ ОСТАТКИ (2026-10-10, STK-09): склад «на дату переезда». Партии
    # заводятся по закупочной цене, но это не закуп периода, не долг
    # поставщику и не касса: материал лежал на полке до системы.
    is_opening = models.BooleanField(_("начальные остатки"), default=False)
    # ВАЛЮТА ЗАКУПА (G1-N3). Суммы строк в сомах (`SupplyLine.cost`) считаются по
    # курсу накладной — так идут партии, закуп и склад; в валюте поставщика
    # сумма строки хранится рядом (`SupplyLine.cost_fc`). Сом — по умолчанию.
    currency = models.CharField(_("валюта"), max_length=3, default="KGS")
    rate = models.DecimalField(
        _("курс, сом за единицу валюты"), max_digits=14, decimal_places=6, default=Decimal("1"),
    )
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

    CURRENCIES = ("KGS", "USD", "EUR", "RUB", "CNY", "KZT")

    @property
    def is_foreign(self) -> bool:
        return self.currency != "KGS"

    @property
    def total_cost(self) -> Decimal:
        """Сумма строк в сомах — то, что система реально приняла на склад."""
        return sum((line.cost for line in self.lines.all()), Decimal("0"))

    @property
    def total_foreign(self):
        """Сумма строк в валюте накладной; для сомовой накладной — None."""
        if not self.is_foreign:
            return None
        total = Decimal("0")
        for line in self.lines.all():
            if line.cost_fc is not None:
                total += line.cost_fc
            elif self.rate:
                total += (line.cost / self.rate).quantize(Decimal("0.01"))
        return total

    @property
    def discrepancy(self) -> Decimal:
        """Бумага минус система. Не ноль — где-то опечатка, и её видно сразу.
        У накладной в валюте сумма по бумаге — в валюте накладной."""
        if self.stated_total is None:
            return Decimal("0")
        if self.is_foreign:
            return self.stated_total - (self.total_foreign or Decimal("0"))
        return self.stated_total - self.total_cost

    # --- Оплата: старое поле + платежи отдельными строками ---------------------
    #
    # `paid_amount` — то, что было записано в самой накладной до 2026-10-10
    # (оплата при приёмке и правка поля). Новые платежи — строки
    # `SupplierPayment`. Прошлое не переносилось: оплачено = старое поле + платежи.
    @property
    def payments_settled(self) -> Decimal:
        """Сколько долга по накладной закрыли платежи-строки, сом."""
        return sum((p.signed_settled for p in self.payments.all()), Decimal("0"))

    @property
    def paid_total(self) -> Decimal:
        return (self.paid_amount or Decimal("0")) + self.payments_settled

    @property
    def debt(self) -> Decimal:
        """Сколько мы ещё должны поставщику по этой накладной, сом."""
        if self.is_opening:
            return Decimal("0")
        owed = self.total_cost - self.paid_total
        return owed if owed > 0 else Decimal("0")

    @property
    def overpaid(self) -> Decimal:
        """Заплачено больше накладной (вернули товар, поправили сумму) — кредит
        у поставщика, сом."""
        if self.is_opening:
            return Decimal("0")
        extra = self.paid_total - self.total_cost
        return extra if extra > 0 else Decimal("0")

    @property
    def paid_foreign(self):
        if not self.is_foreign:
            return None
        paid = Decimal("0")
        for p in self.payments.all():
            if p.amount_fc is not None:
                paid += p.amount_fc if p.kind != SupplierPayment.Kind.REFUND else -p.amount_fc
            elif self.rate:
                paid += (p.signed_settled / self.rate).quantize(Decimal("0.01"))
        if self.rate and self.paid_amount:
            paid += (self.paid_amount / self.rate).quantize(Decimal("0.01"))
        return paid

    @property
    def debt_foreign(self):
        """Долг в валюте накладной; для сомовой накладной — None."""
        if not self.is_foreign or self.is_opening:
            return None
        left = (self.total_foreign or Decimal("0")) - (self.paid_foreign or Decimal("0"))
        # Долг в сомах закрыт — в валюте тоже ноль, даже если на копейки не сошлось.
        if self.debt <= 0:
            return Decimal("0")
        return left if left > 0 else Decimal("0")


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
    # Сумма строки в валюте накладной (если она не сом); `cost` — её пересчёт по
    # курсу накладной.
    cost_fc = models.DecimalField(
        _("сумма строки в валюте"), max_digits=14, decimal_places=2, null=True, blank=True,
    )
    code = models.CharField(_("маркировка партии"), max_length=120, blank=True)
    # Созданная партия — у площадных и штучных материалов (штучные — с STK-01).
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


class SupplierPayment(models.Model):
    """Платёж поставщику — отдельная строка (2026-10-10, cash-06, cash-07).

    Раньше оплата жила двумя полями самой накладной («оплачено» и «чем»): оплата
    с двух счетов сдвигала остатки на сумму, которой в кассе не было, аванс
    поставщику негде было записать, а сальдо не существовало вовсе. Теперь
    каждый платёж — строка со своей датой, суммой, счётом и автором. Привязан к
    накладной либо стоит «авансом без накладной» (`supply` пусто).

    Прошлое не переносилось: «оплачено по накладной» = старое поле
    `Supply.paid_amount` + сумма платежей-строк.

    Три вида:

    * ``PAYMENT`` — деньги ушли поставщику (кассовая запись «Оплата
      поставщику»);
    * ``REFUND`` — поставщик вернул деньги за возвращённый товар (приход в
      кассу той же статьёй);
    * ``OFFSET`` — зачёт аванса в накладную: денег не двигает, только
      переносит часть уже уплаченного аванса на накладную (`source`).

    Валюта (G1-N3): платёж по накладной в валюте задаётся в валюте накладной
    (`amount_fc`) и курсом на день оплаты (`rate`); `amount` — сколько сом
    реально ушло, `fx_diff` — разница с тем, сколько долга это закрыло по курсу
    накладной (плюс — заплатили дороже, расход «Курсовая разница»).
    """

    class Kind(models.TextChoices):
        PAYMENT = "PAYMENT", _("Оплата")
        REFUND = "REFUND", _("Возврат денег от поставщика")
        OFFSET = "OFFSET", _("Зачёт аванса")

    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, null=True, blank=True,
        related_name="payments", verbose_name=_("поставщик"),
    )
    supply = models.ForeignKey(
        Supply, on_delete=models.CASCADE, null=True, blank=True,
        related_name="payments", verbose_name=_("накладная"),
    )
    # Зачёт: из какого аванса взято.
    source = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="offsets", verbose_name=_("аванс"),
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PAYMENT)
    paid_on = models.DateField(_("дата"), default=timezone.localdate)
    account = models.CharField(
        _("счёт"), max_length=10, blank=True,
        choices=[("CASH", _("Наличные")), ("BANK", _("Банк"))],
    )
    amount = models.DecimalField(_("сумма, сом"), max_digits=14, decimal_places=2)
    fx_diff = models.DecimalField(
        _("курсовая разница, сом"), max_digits=14, decimal_places=2, default=Decimal("0"),
    )
    currency = models.CharField(_("валюта"), max_length=3, default="KGS")
    amount_fc = models.DecimalField(
        _("сумма в валюте"), max_digits=14, decimal_places=2, null=True, blank=True,
    )
    rate = models.DecimalField(
        _("курс на дату оплаты"), max_digits=14, decimal_places=6, null=True, blank=True,
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Кассовая запись платежа и трата «Курсовая разница» — чтобы правка одной
    # строки двигала только её деньги.
    cash_entry = models.ForeignKey(
        "finance.CashEntry", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_payments", verbose_name=_("запись кассы"),
    )
    fx_expense = models.ForeignKey(
        "finance.ExpenseEntry", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_payments", verbose_name=_("курсовая разница"),
    )

    class Meta:
        verbose_name = _("платёж поставщику")
        verbose_name_plural = _("платежи поставщикам")
        ordering = ["-paid_on", "-id"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount} от {self.paid_on}"

    @property
    def settled(self) -> Decimal:
        """Сколько долга в сомах эта строка закрывает (без курсовой разницы)."""
        return self.amount - (self.fx_diff or Decimal("0"))

    @property
    def signed_settled(self) -> Decimal:
        """Влияние на оплаченное по накладной: оплата и зачёт — плюс, возврат
        денег — минус."""
        return -self.settled if self.kind == self.Kind.REFUND else self.settled

    @property
    def supplier_effect(self) -> Decimal:
        """Влияние на сальдо поставщика: зачёт аванса сальдо не двигает —
        деньги он уже уменьшил, когда был уплачен."""
        return Decimal("0") if self.kind == self.Kind.OFFSET else self.signed_settled

    @property
    def is_advance(self) -> bool:
        return self.kind == self.Kind.PAYMENT and self.supply_id is None


class SupplierReturn(models.Model):
    """Возврат товара поставщику по накладной (G1-N2).

    Склад уменьшается по партии этой накладной, по цене партии, без потерь в
    ОПиУ; закуп и сумма накладной уменьшаются на стоимость возвращённого. Деньги —
    либо вернулись на счёт (строка `SupplierPayment` вида REFUND), либо остались
    кредитом у поставщика (накладная оплачена больше своей суммы — сальдо это
    показывает).
    """

    supply = models.ForeignKey(
        Supply, on_delete=models.CASCADE, related_name="returns", verbose_name=_("накладная"),
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, null=True, blank=True,
        related_name="returns", verbose_name=_("поставщик"),
    )
    returned_on = models.DateField(_("дата возврата"), default=timezone.localdate)
    amount = models.DecimalField(_("стоимость возвращённого, сом"), max_digits=14, decimal_places=2)
    refund = models.DecimalField(
        _("возвращено деньгами, сом"), max_digits=14, decimal_places=2, default=Decimal("0"),
    )
    refund_account = models.CharField(max_length=10, blank=True)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_returns",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("возврат поставщику")
        verbose_name_plural = _("возвраты поставщикам")
        ordering = ["-returned_on", "-id"]


class SupplierReturnLine(models.Model):
    ret = models.ForeignKey(SupplierReturn, on_delete=models.CASCADE, related_name="lines")
    supply_line = models.ForeignKey(
        SupplyLine, on_delete=models.SET_NULL, null=True, blank=True, related_name="return_lines",
    )
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="supplier_return_lines")
    label = models.CharField(max_length=160, blank=True)
    quantity = models.DecimalField(_("возвращено, в единицах строки"), max_digits=14, decimal_places=4)
    area = models.DecimalField(_("возвращено, кв.м или шт"), max_digits=14, decimal_places=4)
    cost = models.DecimalField(_("стоимость, сом"), max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]


class SupplierOpeningDebt(models.Model):
    """Начальный долг поставщику (STK-09, F6, XL-04): сколько были должны на
    дату переезда, без партий и накладных.

    Входит в сальдо поставщика, но НЕ в закуп периода и не в кассу: деньги
    ещё не платили, материал давно на полке. Минус — поставщик нам должен (наш
    аванс на дату переезда).
    """

    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="opening_debts", verbose_name=_("поставщик"),
    )
    as_of = models.DateField(_("на дату"), default=timezone.localdate)
    amount = models.DecimalField(_("сумма, сом"), max_digits=14, decimal_places=2)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="supplier_opening_debts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("начальный долг поставщику")
        verbose_name_plural = _("начальные долги поставщикам")
        ordering = ["as_of", "id"]
