import uuid
from decimal import Decimal

from PIL import Image, UnidentifiedImageError
from rest_framework import serializers

from .models import (
    InventoryLog,
    Material,
    MaterialImage,
    MaterialMonthOpening,
    MaterialType,
    ProductionSite,
    Roll,
    RollStocktake,
    Supplier,
    SupplierOpeningDebt,
    SupplierPayment,
    SupplierReturn,
    Supply,
    SupplyLine,
)


MAX_BACKDATE_DAYS = 366   # как у заказа задним числом (sales/views.py)


def check_op_day(value, what="Дата операции"):
    """Дата складской операции: не в будущем и не дальше года назад.

    Будущее — денег и материала ещё нет. Дальше года — почти всегда опечатка в
    годе («2025» вместо «2026»), а запись уехала бы в чужой отчёт. Закрытый
    период проверяет вьюха (`finance.periods.ensure_open`): ей нужен 400 с
    текстом про замок, а не про формат даты.
    """
    from datetime import timedelta

    from django.utils import timezone

    if value is None:
        return value
    today = timezone.localdate()
    if value > today:
        raise serializers.ValidationError(f"{what} не может быть в будущем.")
    if value < today - timedelta(days=MAX_BACKDATE_DAYS):
        raise serializers.ValidationError(
            f"{what} — больше года назад. Проверьте год."
        )
    return value


def _sees_money(context) -> bool:
    """Показывать ли закупочные цифры: владельцу и бухгалтеру — да, складовщику
    — нет (те же правила, что у себестоимости и маржи в чеках)."""
    request = context.get("request")
    return bool(request and getattr(request.user, "sees_money", False))


# Фото материала: потолки до обработки. Размер файла и число пикселей проверяем
# ДО того, как Pillow начнёт разбирать картинку: небольшой PNG с заявленными
# 100 000 × 100 000 пикселей занимает килобайты, а при разборе съедает гигабайты
# памяти (бомба распаковки).
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
IMAGE_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}


class SafeImageField(serializers.ImageField):
    """Картинка с потолками по размеру и пикселям и безопасным именем.

    Имя файла берём не от клиента (`../x.php`, юникод, коллизии), а генерируем:
    uuid + расширение по РЕАЛЬНОМУ формату, который определил Pillow, а не по
    тому, что написано в имени.
    """

    def to_internal_value(self, data):
        size = getattr(data, "size", None)
        if size is not None and size > MAX_IMAGE_BYTES:
            raise serializers.ValidationError(
                f"Файл слишком большой: не больше {MAX_IMAGE_BYTES // (1024 * 1024)} МБ."
            )
        fmt = None
        if hasattr(data, "seek") and hasattr(data, "read"):
            try:
                data.seek(0)
                with Image.open(data) as img:     # читает только заголовок
                    width, height = img.size
                    fmt = img.format
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                raise serializers.ValidationError("Загрузите корректное изображение.")
            finally:
                data.seek(0)
            if width * height > MAX_IMAGE_PIXELS:
                raise serializers.ValidationError(
                    "Изображение слишком большое по размерам "
                    f"({width}×{height}). Уменьшите фото."
                )
            if fmt not in IMAGE_EXTENSIONS:
                raise serializers.ValidationError(
                    "Допустимые форматы фото: JPEG, PNG, WEBP, GIF."
                )
        file = super().to_internal_value(data)
        if fmt:
            file.name = f"{uuid.uuid4().hex}.{IMAGE_EXTENSIONS[fmt]}"
        return file


class MaterialImageSerializer(serializers.ModelSerializer):
    image = SafeImageField()

    class Meta:
        model = MaterialImage
        fields = ["id", "material", "image", "is_primary", "uploaded_at"]
        read_only_fields = ["uploaded_at"]


class PriceTierSerializer(serializers.Serializer):
    """Ступень опта: «от N листов — цена за лист» (CLI-02)."""

    min_qty = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))


class MaterialSerializer(serializers.ModelSerializer):
    images = MaterialImageSerializer(many=True, read_only=True)
    # Ступени опта (CLI-02, волна 2). Прислали список — он заменяет прежний.
    price_tiers = PriceTierSerializer(many=True, required=False)

    def validate_price_tiers(self, value):
        seen = set()
        for row in value:
            if row["min_qty"] in seen:
                raise serializers.ValidationError(f"Порог «от {row['min_qty']}» повторяется.")
            seen.add(row["min_qty"])
        return sorted(value, key=lambda r: r["min_qty"])

    def _set_tiers(self, material, tiers):
        from .models import MaterialPriceTier

        material.price_tiers.all().delete()
        MaterialPriceTier.objects.bulk_create(
            MaterialPriceTier(material=material, min_qty=r["min_qty"], price=r["price"]) for r in tiers
        )
        # Список мог быть подгружен заранее (prefetch) — иначе журнал и ответ
        # увидели бы старые ступени.
        getattr(material, "_prefetched_objects_cache", {}).pop("price_tiers", None)

    def create(self, validated_data):
        tiers = validated_data.pop("price_tiers", None)
        material = super().create(validated_data)
        if tiers is not None:
            self._set_tiers(material, tiers)
        return material

    def update(self, instance, validated_data):
        tiers = validated_data.pop("price_tiers", None)
        material = super().update(instance, validated_data)
        if tiers is not None:
            self._set_tiers(material, tiers)
        return material
    primary_image = serializers.SerializerMethodField()
    is_below_critical = serializers.BooleanField(read_only=True)
    sqm_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    sheets_remaining = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, allow_null=True
    )
    # Остаток рулона в погонных метрах — владелец меряет рулон метрами.
    metres_remaining = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, allow_null=True
    )
    sells_by_metre = serializers.BooleanField(read_only=True)
    # Рулон продаётся ещё и по кв.м изделия (CALC-10): задана цена за кв.м.
    sells_roll_by_area = serializers.BooleanField(read_only=True)
    stock_value = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    type_name = serializers.CharField(source="type.name", read_only=True)
    production_name = serializers.CharField(source="production.name", read_only=True)
    # Подсказка для формы: как назвался бы материал по заполненным полям.
    suggested_name = serializers.CharField(read_only=True)
    # Закуп последней партии, цена по наценке и маржа (STK-03) — только тем,
    # кто видит деньги.
    pricing = serializers.SerializerMethodField()
    # Остаток и стоимость по площадкам хранения (STK-05). Пусто — площадки не
    # указаны нигде.
    by_site = serializers.SerializerMethodField()

    def get_by_site(self, obj):
        from .sites import site_stock

        names = self.context.get("_site_names")
        if names is None:
            names = {s.pk: s.name for s in ProductionSite.objects.all()}
            self.context["_site_names"] = names
        rows = site_stock(obj, names)
        if not _sees_money(self.context):
            for row in rows:
                row["value"] = None
        return rows

    # Остаток и минимум в единицах материала (STK-06): листы, метры, штуки.
    stock_units = serializers.SerializerMethodField()

    def get_stock_units(self, obj):
        from .reorder import min_in_units, stock_in_units, unit_kind, unit_label

        return {
            "kind": unit_kind(obj), "label": unit_label(obj),
            "stock": stock_in_units(obj), "min": min_in_units(obj),
        }

    def get_pricing(self, obj):
        if not _sees_money(self.context):
            return None
        from .pricing import pricing_hint

        return pricing_hint(obj)

    class Meta:
        model = Material
        fields = [
            "id",
            "name",
            "type",
            "type_name",
            "thickness_mm",
            "color",
            "article",
            "sheet_width",
            "sheet_height",
            "suggested_name",
            "unit",
            "is_roll_material",
            "intake_form",
            "quantity",
            "critical_balance",
            "purchase_price",
            "price_per_unit",
            "price_per_sqm",
            "piece_price",
            "piece_area",
            "wholesale_price",
            "wholesale_min_qty",
            "price_tiers",
            "cut_rate_per_pm",
            "markup_percent",
            "pricing",
            "kim_percent",
            "min_stock",
            "reorder_to",
            "stock_units",
            "by_site",
            "roll_width",
            "price_per_pm",
            "metres_remaining",
            "sells_by_metre",
            "sells_roll_by_area",
            "production",
            "production_name",
            "sqm_price",
            "sheets_remaining",
            "is_below_critical",
            "is_archived",
            "stock_value",
            "images",
            "primary_image",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["quantity", "created_at", "updated_at"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Закупочная цена и стоимость склада — админу и бухгалтеру; складовщику
        # приходит null: он оформляет и принимает товар, а почём цех купил,
        # ему знать незачем (те же правила, что у себестоимости в чеках).
        # Поле остаётся записываемым: карточку с закупкой правит админ.
        if not _sees_money(self.context):
            data["purchase_price"] = None
            data["stock_value"] = None
            # Наценка вместе с ценой продажи выдаёт закуп.
            data["markup_percent"] = None
        return data

    def get_primary_image(self, obj):
        request = self.context.get("request")
        primary = next((img for img in obj.images.all() if img.is_primary), None)
        primary = primary or obj.images.first()
        if not primary:
            return None
        url = primary.image.url
        return request.build_absolute_uri(url) if request else url

    def validate(self, attrs):
        # У формы «Рулон» ширина обязательна. Раньше пустая ширина ничем не
        # отличалась от заполненной на этапе сохранения — а дальше материал
        # МОЛЧА возвращался к продаже по площади (четыре вкладки, ширина как
        # свободное поле в кассе): ровно та ошибка, от которой уходили, только
        # спрятанная за незаполненным полем. Теперь продажа метрами не зависит
        # от ширины (решает форма), а незаполненная ширина — ошибка ввода,
        # которую видно при сохранении карточки, а не через месяц в чеке.
        # При частичном обновлении недостающие поля берём у самой записи.
        def current(name):
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None) if self.instance is not None else None

        check_card_numbers(attrs)
        kim = attrs.get("kim_percent")
        if kim is not None and not (Decimal("1") <= kim <= Decimal("100")):
            raise serializers.ValidationError(
                {"kim_percent": "КИМ — от 1 до 100 %. Пусто — списывать ровно площадь деталей."}
            )
        markup = attrs.get("markup_percent")
        if markup is not None and not (Decimal("-90") <= markup <= Decimal("1000")):
            raise serializers.ValidationError({"markup_percent": "Наценка — от −90 до 1000 %."})
        reorder_to = attrs.get("reorder_to")
        if reorder_to is not None and reorder_to < 0:
            raise serializers.ValidationError(
                {"reorder_to": "«Заказывать до» не может быть меньше нуля. Пусто — до двух минимумов."}
            )

        is_roll = current("is_roll_material")
        form = current("intake_form")
        width = current("roll_width")
        if is_roll and form == Material.IntakeForm.ROLL and not (width and Decimal(width) > 0):
            raise serializers.ValidationError(
                {"roll_width": "У рулона укажите ширину, м: она подставляется в "
                               "приёмку и без неё рулон не принять."}
            )

        # У формы «Лист» обязателен размер листа — по той же причине, что ширина
        # у рулона. Без него `piece_area` остаётся нулём, а из неё считается всё,
        # что владелец про лист и спрашивает: сколько листов лежит на складе и
        # почём обходится лист по закупке. Обе строки просто НЕ показывались, и
        # выглядело это не как «данных не хватает», а как «система не умеет».
        #
        # Особенно легко попасть, переключив штучный материал на лист: размера у
        # него отродясь не было, а форма его не требовала.
        #
        # Площадь можно задать и напрямую (нестандартный лист без размеров) —
        # поэтому проверяем именно её, а не ширину с высотой.
        if is_roll and form == Material.IntakeForm.SHEET:
            area = current("piece_area")
            w, h = current("sheet_width"), current("sheet_height")
            if w and h:
                area = Decimal(w) * Decimal(h)
            if not (area and Decimal(area) > 0):
                raise serializers.ValidationError(
                    {"sheet_width": "У листа укажите размер, м (ширина и высота): "
                                    "из него считается остаток в листах и цена "
                                    "закупки за лист."}
                )
        return attrs


# Цены и ставки карточки не бывают отрицательными, а размер листа и ширина
# рулона больше 4 м — это сантиметры («122 × 244»), вставленные из Excel
# (XL-01, перепроверка 10.10). Раньше и то и другое молча сохранялось: рез по
# −65 сом/пог.м уменьшал сумму заказа, лист 122×244 давал 29 768 кв.м.
NON_NEGATIVE_FIELDS = (
    "price_per_sqm", "piece_price", "price_per_unit", "price_per_pm", "purchase_price",
    "cut_rate_per_pm", "wholesale_price", "wholesale_min_qty", "thickness_mm",
    "critical_balance",
)
METRE_FIELDS = ("sheet_width", "sheet_height", "roll_width")
MAX_METRES = Decimal("4")


def check_card_numbers(attrs) -> None:
    errors = {}
    for name in NON_NEGATIVE_FIELDS:
        value = attrs.get(name)
        if value is not None and value < 0:
            errors[name] = "Не может быть меньше нуля."
    for name in METRE_FIELDS:
        value = attrs.get(name)
        if value is not None and value < 0:
            errors[name] = "Не может быть меньше нуля."
        elif value is not None and value > MAX_METRES:
            errors[name] = (
                f"{format(value.normalize(), 'f')} м — больше 4 м. Размер вводится в метрах: "
                "122 см — это 1.22."
            )
    if errors:
        raise serializers.ValidationError(errors)


def build_ref_index(queryset):
    """Справочник для сетки: и по ключу, и по названию без учёта регистра.

    Регистр сводим в Python, а не запросом `name__iexact`: в SQLite он
    складывает только латиницу, поэтому «форекс» не находил «Форекс» на деве и
    находил на проде (PostgreSQL). Расхождение dev/prod ровно в том месте, где
    заказчик вставляет свои названия строчными буквами.
    """
    index = {}
    for obj in queryset:
        index[str(obj.pk)] = obj
        index[obj.name.strip().casefold()] = obj
    return index


class RefByIdOrNameField(serializers.Field):
    """Ссылка на справочник: принимает и id, и название.

    В сетке массового ввода ячейка «Тип» — выпадающий список (приходит id), но
    туда же вставляют кусок таблицы из Excel, где написано «Форекс» текстом.
    Требовать от заказчика ключи вместо названий было бы издевательством.

    Справочники приходят готовым индексом в контексте — иначе на пачке в 50
    строк это 100 лишних запросов.
    """

    def __init__(self, context_key, label, **kwargs):
        self.context_key = context_key
        self.label_text = label
        super().__init__(**kwargs)

    def to_representation(self, value):
        return value.pk if value else None

    def to_internal_value(self, data):
        if data in (None, ""):
            return None
        text = str(data).strip()
        found = (self.context.get(self.context_key) or {}).get(text.casefold())
        if not found:
            raise serializers.ValidationError(f"{self.label_text} «{text}» не найден.")
        return found


class MaterialBulkRowSerializer(serializers.ModelSerializer):
    """Одна строка сетки массового ввода каталога.

    Отличия от обычного `MaterialSerializer`:
    - название необязательно — пустое соберётся из полей (`Material.save`);
    - тип и производство принимаются названием, а не только ключом;
    - единица измерения выводится из размера листа, если её не указали.
    """

    name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    type = RefByIdOrNameField("types", "Тип", required=False, allow_null=True)
    production = RefByIdOrNameField(
        "sites", "Производство", required=False, allow_null=True
    )

    class Meta:
        model = Material
        fields = [
            "name", "type", "thickness_mm", "color", "article",
            "sheet_width", "sheet_height", "unit", "is_roll_material",
            "intake_form", "roll_width", "price_per_pm",
            "critical_balance", "purchase_price", "price_per_unit",
            "price_per_sqm", "piece_price", "cut_rate_per_pm",
            "wholesale_price", "wholesale_min_qty", "production",
        ]

    NUMERIC = (
        "thickness_mm", "sheet_width", "sheet_height", "roll_width", "price_per_pm",
        "critical_balance", "purchase_price", "price_per_unit", "price_per_sqm",
        "piece_price", "cut_rate_per_pm", "wholesale_price", "wholesale_min_qty",
    )

    def to_internal_value(self, data):
        # Вставка из русского Excel (XL-01): «1,22», «2 679», «2 679,50 сом».
        from .numbers import normalize_numbers

        return super().to_internal_value(normalize_numbers(data, self.NUMERIC))

    def validate(self, attrs):
        # Форму выводим из ЗАПОЛНЕННЫХ полей, без отдельной колонки «форма»:
        # ширина рулона стоит — значит рулон, размер листа — значит лист. Это
        # тот же приём, которым лист уже определялся, и он экономит колонку в
        # сетке, где их и так одиннадцать.
        check_card_numbers(attrs)
        has_sheet = attrs.get("sheet_width") and attrs.get("sheet_height")
        has_roll = attrs.get("roll_width")
        if has_roll and has_sheet:
            raise serializers.ValidationError(
                {"roll_width": "У рулона размера листа не бывает: оставьте "
                               "что-то одно — размер листа или ширину рулона."}
            )
        if has_roll:
            # Рулон продаётся ДЛИНОЙ, и цена за метр — единственная, по которой
            # его можно продать: без неё касса откажет уже на первой продаже.
            if not attrs.get("price_per_pm"):
                raise serializers.ValidationError(
                    {"price_per_pm": "У рулона нужна цена за пог.м — по ней он и продаётся."}
                )
            attrs["is_roll_material"] = True
            attrs["intake_form"] = Material.IntakeForm.ROLL
            attrs["unit"] = Material.Unit.SQM
        elif has_sheet and "is_roll_material" not in self.initial_data:
            attrs["is_roll_material"] = True
        if attrs.get("is_roll_material") and "unit" not in self.initial_data:
            attrs["unit"] = Material.Unit.SQM

        probe = Material(**{k: v for k, v in attrs.items()})
        name = (attrs.get("name") or "").strip() or probe.suggested_name()
        if not name:
            raise serializers.ValidationError(
                {"name": "Пустая строка: заполните название или тип с цветом."}
            )
        attrs["name"] = name
        # Занятость названия проверяет вьюха: там же ловятся дубли внутри самой
        # пачки, и обе проверки лежат в одном месте.
        return attrs


class MaterialPriceUpdateSerializer(serializers.Serializer):
    """Payload for PATCH .../update-price/ — admin retail-price change."""

    price_per_unit = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


class InventoryLogSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True
    )
    material_name = serializers.CharField(source="material.name", read_only=True)
    material_unit = serializers.CharField(source="material.unit", read_only=True)
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    # Номер заказа, а не UUID: в ленте движений он и показывается.
    order_number = serializers.IntegerField(source="receipt.order_number", read_only=True)
    # Себестоимость движения (списание, отход) — только тем, кто видит деньги:
    # складовщик записывает брак, но почём цех его купил, ему знать незачем.
    cost = serializers.SerializerMethodField()
    # Рулонный ли материал — ленте отходов нужна единица без второго запроса.
    material_is_roll = serializers.BooleanField(source="material.is_roll_material", read_only=True)

    # Возврат поставщику — своя строка «Движения» с минусом и стоимостью
    # (RU-N23); тип в базе — корректировка, экран подписывает его отдельно.
    supplier_return = serializers.SerializerMethodField()

    def get_cost(self, obj):
        return obj.cost if _sees_money(self.context) else None

    def get_supplier_return(self, obj):
        return obj.type == InventoryLog.Type.CORRECTION and (obj.reason or "").startswith("Возврат поставщику")

    class Meta:
        model = InventoryLog
        fields = [
            "id",
            "type",
            "type_display",
            "supplier_return",
            "material",
            "material_name",
            "material_unit",
            "material_is_roll",
            "quantity_changed",
            # Метры у рулона — чем операцию мерили на самом деле. Пусто у
            # листа и штучного: там мера и есть та, что в quantity_changed.
            "metres_changed",
            "actual_price",
            "cost",
            "reason",
            "receipt",
            "order_number",
            "created_by",
            "created_by_username",
            "happened_at",
        ]
        read_only_fields = ["created_by"]


class QuickIntakeSerializer(serializers.Serializer):
    """Быстрый приход одной позиции — кнопка «Поступление» на строке материала.

    Остаётся рядом с накладной: одна банка клея, привезённая между делом,
    документа не заслуживает. Имя Supply* отдано приходной НАКЛАДНОЙ.
    """

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    actual_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # Дата поступления: приход часто вносят задним числом. Не указана — сегодня.
    happened_on = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True)
    # Чем заплатили за поставку: «наличные» / «банк» пишут расход в кассу,
    # «в долг» — не пишут. Поля нет — тоже не пишем: система не должна
    # выдумывать движение денег за того, кто про него ничего не сказал (так
    # ведут себя старые вызовы API и импорт каталога). Приход без указанной
    # оплаты — долг поставщику (D-195), как «в долг».
    payment = serializers.ChoiceField(
        choices=["CASH", "BANK", "DEBT"], required=False, allow_blank=True
    )



def _sheets_to_area(material, sheets, field):
    """Листы → кв.м по площади листа карточки, без округления до сотых
    (XL-03: «6 листов» = 17.8608, а не 17.86)."""
    if not (material.is_roll_material and material.piece_area and material.piece_area > 0):
        raise serializers.ValidationError(
            {field: f"У «{material.name}» не задан размер листа — вводите количество в кв.м."}
        )
    return (Decimal(sheets) * material.piece_area).quantize(Decimal("0.0001"))


class _NumbersMixin:
    """Числа с запятой и пробелами (как пишет русский Excel) — в точку."""

    NUMERIC: tuple = ()

    def to_internal_value(self, data):
        from .numbers import normalize_numbers

        return super().to_internal_value(normalize_numbers(data, self.NUMERIC))


class AdjustmentSerializer(_NumbersMixin, serializers.Serializer):
    """Inventory adjustment — reconcile actual vs system stock.

    Количество — кв.м до 4 знаков (`counted_quantity`) или ЛИСТАМИ
    (`counted_sheets`) у листового материала: сервер переводит листы по площади
    листа без округления. Раньше форма слала кв.м с 2 знаками, и «6 листов»
    1.22×2.44 становились 17.86 вместо 17.8608 — последний лист было не продать
    (XL-03).
    """

    NUMERIC = ("counted_quantity", "counted_sheets")

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    counted_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True,
    )
    counted_sheets = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True,
    )
    reason = serializers.CharField(required=False, allow_blank=True)
    # Дата пересчёта (G3-N3): недостача ложится в месяц пересчёта, а не ввода.
    happened_on = serializers.DateField(required=False, allow_null=True)

    def validate_happened_on(self, value):
        return check_op_day(value, "Дата пересчёта")

    def validate(self, attrs):
        sheets = attrs.get("counted_sheets")
        qty = attrs.get("counted_quantity")
        if (sheets is None) == (qty is None):
            raise serializers.ValidationError(
                {"counted_quantity": "Укажите пересчитанное количество — в кв.м или листами."}
            )
        if sheets is not None:
            attrs["counted_quantity"] = _sheets_to_area(attrs["material"], sheets, "counted_sheets")
        return attrs


class WriteOffSerializer(_NumbersMixin, serializers.Serializer):
    """Write off stock for damage / defect / loss / expiry.

    Количество — до 4 знаков или листами (`sheets`) у листового материала;
    хвост округления ≤ 0.01 кв.м при списании последнего листа уходит в ноль
    (`rolls.snap_tail`, STK-08): раньше 2.9768 не принималось, 2.98 было
    «больше остатка», а 2.97 оставляло 0.0068 кв.м пыли.
    """

    NUMERIC = ("quantity", "sheets")

    REASONS = {
        "DAMAGE": "Порча",
        "DEFECT": "Брак",
        "LOSS": "Утеря",
        "EXPIRY": "Истёк срок",
        "OTHER": "Прочее",
    }

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True,
        help_text="Списываемое количество (положительное число)",
    )
    sheets = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True,
    )
    reason_code = serializers.ChoiceField(choices=list(REASONS.keys()))
    note = serializers.CharField(required=False, allow_blank=True)
    # Дата списания (брак нашли вчера — вносят сегодня). Пусто — сегодня.
    # Закрытый период держит вьюха (F4/PNL-01): списание датой принятого месяца
    # меняло бы его прибыль.
    happened_on = serializers.DateField(required=False, allow_null=True)

    def validate_happened_on(self, value):
        return check_op_day(value, "Дата списания")

    def validate(self, attrs):
        from .rolls import snap_tail

        material = attrs["material"]
        sheets, qty = attrs.get("sheets"), attrs.get("quantity")
        if (sheets is None) == (qty is None):
            raise serializers.ValidationError(
                {"quantity": "Укажите, сколько списать — в единицах материала или листами."}
            )
        if sheets is not None:
            qty = _sheets_to_area(material, sheets, "sheets")
        if qty <= 0:
            raise serializers.ValidationError({"quantity": "Количество должно быть больше нуля."})
        qty = snap_tail(material, qty)
        if qty > material.quantity:
            raise serializers.ValidationError(
                {"quantity": f"Нельзя списать больше, чем на складе ({material.quantity})."}
            )
        attrs["quantity"] = qty
        return attrs

    def reason_text(self) -> str:
        label = self.REASONS[self.validated_data["reason_code"]]
        note = self.validated_data.get("note")
        return f"Списание: {label}." + (f" {note}" if note else "")


class RollSerializer(serializers.ModelSerializer):
    # Себестоимость партии (за кв.м, за метр, всей) — только тем, кто видит
    # деньги: список рулонов грузит и касса складовщика, а закупка в подписи
    # рулона («№8 · 2 м · 200 сом/м») ему ни к чему.
    cost_per_sqm = serializers.SerializerMethodField()
    cost_per_pm = serializers.SerializerMethodField()
    purchase_cost = serializers.SerializerMethodField()
    # Долг поставщику за партию — тоже деньги, складовщику не показываем.
    supplier_debt = serializers.SerializerMethodField()
    material_name = serializers.CharField(source="material.name", read_only=True)
    production_name = serializers.CharField(
        source="production.name", read_only=True, default=None
    )
    dimensions_label = serializers.CharField(read_only=True)
    metres_initial = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, allow_null=True
    )
    metres_remaining = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, allow_null=True
    )
    shortfall = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, allow_null=True
    )

    # Площадка хранения и где лежат части партии (STK-05).
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    placements = serializers.SerializerMethodField()

    def get_placements(self, obj):
        from .sites import placements

        names = self.context.get("_site_names")
        if names is None:
            names = {s.pk: s.name for s in ProductionSite.objects.all()}
            self.context["_site_names"] = names
        return [
            {"site": sid, "name": names.get(sid) if sid else None, "area": area}
            for sid, area in placements(obj).items()
        ]

    # Накладная партии (если пришла документом) — карточке партии и кнопке
    # «Исправить приход»: исправление двигает и сумму накладной.
    supply = serializers.SerializerMethodField()
    supply_number = serializers.SerializerMethodField()

    def _line(self, obj):
        try:
            return obj.supply_line
        except SupplyLine.DoesNotExist:
            return None

    def get_supply(self, obj):
        line = self._line(obj)
        return line.supply_id if line else None

    def get_supply_number(self, obj):
        line = self._line(obj)
        return (line.supply.number or f"#{line.supply_id}") if line else None

    def get_cost_per_sqm(self, obj):
        return obj.cost_per_sqm if _sees_money(self.context) else None

    def get_cost_per_pm(self, obj):
        return obj.cost_per_pm if _sees_money(self.context) else None

    def get_purchase_cost(self, obj):
        return obj.purchase_cost if _sees_money(self.context) else None

    def get_supplier_debt(self, obj):
        """Долг поставщику за одиночную партию — расчётом, как в карточке «Долг
        поставщикам» (D-195): сумма − заплачено по кассе, в том числе у старого
        прихода без отметки «в долг». У партии из накладной — 0 (долг в ней)."""
        if not _sees_money(self.context):
            return None
        debts = self.context.get("_lot_debts")
        if debts is None:
            from .supplier_debts import lot_debts, standalone_lots

            debts = {pk: debt for pk, (debt, _m) in lot_debts(standalone_lots()).items()}
            self.context["_lot_debts"] = debts
        return max(debts.get(obj.pk, Decimal("0")), Decimal("0"))

    class Meta:
        model = Roll
        fields = [
            "id",
            "material",
            "material_name",
            "code",
            # Производство партии — и id, и название: подпись партии в кассе
            # («бишкек · 26,54 лист.») читает человек, а не машина.
            "production",
            "production_name",
            "site",
            "site_name",
            "placements",
            "form",
            "width",
            "length",
            "height",
            "sheet_count",
            "dimensions_label",
            "initial_area",
            "remaining_area",
            # Рулон в метрах — в чём его меряет цех. Плюс заявленное поставщиком
            # и недостача: без пары «заявлено / принято» недолив невидим.
            "metres_initial",
            "metres_remaining",
            "cost_per_pm",
            "declared_length",
            "shortfall",
            "purchase_cost",
            "cost_per_sqm",
            "supplier_debt",
            "received_at",
            "supply",
            "supply_number",
        ]


class RollIntakeSerializer(serializers.Serializer):
    """Receive a lot (roll or sheets) for an area-material.

    ROLL  → width × length = area.
    SHEET → width × height × sheet_count = area.
    """

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    code = serializers.CharField(required=False, allow_blank=True)
    # Откуда приехала эта партия. Не прислали — `receive_lot` подставит
    # производство из карточки материала. Раньше это писали словом в
    # маркировку («бишкек»), и свести по производству было нельзя.
    production = serializers.PrimaryKeyRelatedField(
        queryset=ProductionSite.objects.all(), required=False, allow_null=True
    )
    # Где партия будет лежать (STK-05). Пусто — площадка не указана.
    site = serializers.PrimaryKeyRelatedField(
        queryset=ProductionSite.objects.all(), required=False, allow_null=True
    )
    # Чем заплатили за поставку: «наличные» / «банк» пишут расход в кассу,
    # «в долг» — не пишут. Поля нет — тоже не пишем: система не должна
    # выдумывать движение денег за того, кто про него ничего не сказал (так
    # ведут себя старые вызовы API и импорт каталога). Приход без указанной
    # оплаты — долг поставщику (D-195), как «в долг».
    payment = serializers.ChoiceField(
        choices=["CASH", "BANK", "DEBT"], required=False, allow_blank=True
    )
    form = serializers.ChoiceField(choices=Roll.Form.values)
    width = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    length = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    height = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    sheet_count = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    # Полная стоимость партии. У рулона её можно не считать в уме: достаточно
    # цены за метр — поставщик именно так и выставляет счёт.
    purchase_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    cost_per_pm = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # Сколько метров ЗАЯВИЛ поставщик (в `length` — принятое по факту).
    declared_length = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # Дата поступления партии — по ней же идёт FIFO.
    received_on = serializers.DateField(required=False, allow_null=True)
    # Площадь ПРЯМО В КВ.М — когда поставщик выставил счёт квадратами, а не
    # листами. Так приходит обрез и остатки: «45,3 кв.м по 700» и никаких
    # «сколько это листов». Размеры и количество тогда не нужны, площадь берётся
    # как есть, а не считается из них.
    area = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True
    )
    # Цена за кв.м — вторая половина того же способа: партия = площадь × цена.
    cost_per_sqm = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )

    def validate(self, attrs):
        # Приём площадью: размеры не спрашиваем вовсе. Партия знает свою площадь,
        # а этого хватает и для FIFO, и для себестоимости.
        if attrs.get("area") not in (None, ""):
            if attrs["area"] <= 0:
                raise serializers.ValidationError({"area": "Площадь должна быть больше нуля."})
            # У РУЛОНА площадь сама по себе бесполезна: остаток в метрах
            # считается делением площади на ширину ПАРТИИ, и рулон без ширины
            # выпадает из продажи метрами совсем — площадь на складе числится, а
            # продать её нечем. Интерфейс переводит площадь в ширину × длину сам,
            # но ручка обязана держаться и без него.
            if attrs["form"] == Roll.Form.ROLL:
                width = attrs.get("width") or attrs["material"].roll_width
                if not width:
                    raise serializers.ValidationError(
                        {"width": "У рулона площадь принимается только с шириной: "
                                  "из неё считаются метры."}
                    )
                attrs["width"] = width
                attrs["length"] = (attrs["area"] / width).quantize(Decimal("0.01"))
            if attrs.get("purchase_cost") in (None, ""):
                per_sqm = attrs.get("cost_per_sqm")
                if per_sqm in (None, ""):
                    raise serializers.ValidationError(
                        {"purchase_cost": "Укажите стоимость закупки или цену за кв.м."}
                    )
                attrs["purchase_cost"] = (per_sqm * attrs["area"]).quantize(Decimal("0.01"))
            return attrs

        if attrs["form"] == Roll.Form.ROLL:
            # Ширину можно не вводить: она подставляется из карточки материала и
            # ЗАМОРАЖИВАЕТСЯ в партии. Правка опечатки в справочнике потом не
            # должна пересчитывать уже принятые рулоны.
            if not attrs.get("width"):
                attrs["width"] = attrs["material"].roll_width
            if not attrs.get("width") or not attrs.get("length"):
                raise serializers.ValidationError("Для рулона укажите ширину и длину.")
        else:  # SHEET
            if not attrs.get("width") or not attrs.get("height") or not attrs.get("sheet_count"):
                raise serializers.ValidationError(
                    "Для листа укажите ширину, высоту и количество листов."
                )

        # Стоимость: либо полная сумма, либо цена за метр (только у рулона).
        # Считать 12 000 ÷ 45 = 266.67 в уме владелец не должен — от этого
        # деления мы ушли в продаже, и в закупе оно тем более ни к чему:
        # округлив 266.67 до 266, он врёт себе в себестоимости каждого метра.
        if attrs.get("purchase_cost") in (None, ""):
            per_pm = attrs.get("cost_per_pm")
            if per_pm in (None, "") or attrs["form"] != Roll.Form.ROLL:
                raise serializers.ValidationError(
                    {"purchase_cost": "Укажите стоимость закупки или цену за пог.м."}
                )
            attrs["purchase_cost"] = (per_pm * attrs["length"]).quantize(Decimal("0.01"))
        return attrs


class RollWriteOffSerializer(serializers.Serializer):
    """Списание С КОНКРЕТНОГО рулона — в метрах: порвали 2 м рулона №8, а не
    «2 кв.м материала откуда-нибудь»."""

    metres = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    reason_code = serializers.ChoiceField(choices=list(WriteOffSerializer.REASONS.keys()))
    note = serializers.CharField(required=False, allow_blank=True)
    happened_on = serializers.DateField(required=False, allow_null=True)

    def validate_happened_on(self, value):
        return check_op_day(value, "Дата списания")

    def validate_metres(self, value):
        if value <= 0:
            raise serializers.ValidationError("Укажите, сколько метров списать.")
        return value

    def reason_text(self) -> str:
        label = WriteOffSerializer.REASONS[self.validated_data["reason_code"]]
        note = self.validated_data.get("note")
        return f"Списание: {label}." + (f" {note}" if note else "")


class RollStocktakeSerializer(serializers.ModelSerializer):
    """Акт промера — только на чтение: он документ, а не запись, которую правят."""

    roll_label = serializers.SerializerMethodField()
    material_name = serializers.CharField(source="roll.material.name", read_only=True)
    reason_display = serializers.CharField(source="get_reason_code_display", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )

    class Meta:
        model = RollStocktake
        fields = [
            "id", "roll", "roll_label", "material_name",
            "expected_metres", "counted_metres", "difference",
            "reason_code", "reason_display", "note",
            "created_by", "created_by_name", "created_at",
        ]

    def get_roll_label(self, obj):
        return obj.roll.code or f"№{obj.roll_id}"


class RollStocktakeInputSerializer(serializers.Serializer):
    """Промер одного рулона рулеткой."""

    counted_metres = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0
    )
    reason_code = serializers.ChoiceField(choices=RollStocktake.Reason.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        # «Прочее» без объяснения — это та же потерянная причина, от которой
        # акт и заводился: через месяц строка «прочее, −1.5 м» ничего не скажет.
        if attrs["reason_code"] == RollStocktake.Reason.OTHER and not (attrs.get("note") or "").strip():
            raise serializers.ValidationError(
                {"note": "Для причины «Прочее» напишите, что случилось."}
            )
        return attrs


class MaterialMonthOpeningSerializer(_NumbersMixin, serializers.ModelSerializer):
    """Остаток материала на начало месяца — ручной ввод, как в Excel."""

    NUMERIC = ("quantity",)

    material_name = serializers.CharField(source="material.name", read_only=True)

    class Meta:
        model = MaterialMonthOpening
        fields = ["id", "material", "material_name", "year", "month", "quantity", "updated_at"]
        read_only_fields = ["updated_at"]
        # Уникальность (материал, год, месяц) в модели есть, но проверять её
        # здесь нельзя: вьюха делает upsert, а валидатор отклонял бы повторный
        # ввод той же клетки как «уже существует».
        validators = []

    def validate_month(self, value):
        if not 1 <= value <= 12:
            raise serializers.ValidationError("Месяц должен быть от 1 до 12.")
        return value


class MaterialTypeSerializer(serializers.ModelSerializer):
    """Тип материала: Форекс, Акрил, Оргстекло… Справочник, а не список в коде."""

    materials_count = serializers.SerializerMethodField()

    class Meta:
        model = MaterialType
        fields = ["id", "code", "name", "is_builtin", "position", "is_archived", "materials_count"]
        read_only_fields = ["code", "is_builtin"]

    def get_materials_count(self, obj) -> int:
        annotated = getattr(obj, "materials_total", None)
        return annotated if annotated is not None else obj.materials.count()

    def create(self, validated_data):
        validated_data["code"] = MaterialType.make_code(validated_data.get("name", ""))
        validated_data["is_builtin"] = False
        return super().create(validated_data)


class ProductionSiteSerializer(serializers.ModelSerializer):
    """Откуда возят материал: Бишкек, Глобал. Справочник, а не свободный текст."""

    materials_count = serializers.SerializerMethodField()

    class Meta:
        model = ProductionSite
        fields = ["id", "code", "name", "is_builtin", "position", "is_archived", "materials_count"]
        read_only_fields = ["code", "is_builtin"]

    def get_materials_count(self, obj) -> int:
        annotated = getattr(obj, "materials_total", None)
        return annotated if annotated is not None else obj.materials.count()

    def create(self, validated_data):
        validated_data["code"] = ProductionSite.make_code(validated_data.get("name", ""))
        validated_data["is_builtin"] = False
        return super().create(validated_data)


class SupplierSerializer(serializers.ModelSerializer):
    """Справочник поставщиков. Читают все, правит любой, кто принимает товар:
    новую фирму заводит складовщик прямо в накладной.

    Деньги (долг, сальдо, аванс) — только администратору и бухгалтеру:
    складовщику приходит null (STAFF-07)."""

    supplies_count = serializers.SerializerMethodField()
    debt = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()

    class Meta:
        model = Supplier
        fields = [
            "id", "name", "phone", "inn", "note", "is_archived",
            "supplies_count", "debt", "balance",
        ]
        # Уникальность проверяем сами (ниже), без регистра и лишних пробелов, с
        # человеческим текстом: стандартное «поставщик с таким название уже
        # существует» не склонялось и не ловило «глобал» против «Глобал».
        extra_kwargs = {"name": {"validators": []}}

    def validate_name(self, value):
        clean = (value or "").strip()
        if not clean:
            raise serializers.ValidationError("Укажите название поставщика.")
        # Сравниваем в Python: `iexact` в SQLite не видит регистра кириллицы
        # («Глобал» и «глобал» для него разные), а справочник маленький.
        qs = Supplier.objects.all()
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        wanted = clean.casefold()
        for name in qs.values_list("name", flat=True):
            if name.strip().casefold() == wanted:
                raise serializers.ValidationError(f"Поставщик «{name}» уже есть в справочнике.")
        return clean

    def get_supplies_count(self, obj) -> int:
        return len(obj.supplies.all())

    def get_debt(self, obj):
        """Сколько мы должны этому поставщику по его накладным (с платежами)."""
        if not _sees_money(self.context):
            return None
        return sum((s.debt for s in obj.supplies.all()), Decimal("0"))

    def get_balance(self, obj):
        """Сальдо: начальный долг + накладные − платежи. Плюс — мы должны,
        минус — деньги лежат у поставщика (аванс, переплата)."""
        if not _sees_money(self.context):
            return None
        from .supplier_ledger import supplier_balance

        return supplier_balance(obj)


NEGATIVE_LINE = (
    "Сумма строки не может быть меньше нуля. Товар назад поставщику — кнопка «Вернуть "
    "поставщику» в накладной, по которой он пришёл; у накладной закрытого месяца возврат "
    "пройдёт датой возврата, открывать месяц не нужно."
)


class SupplyLineSerializer(_NumbersMixin, serializers.ModelSerializer):
    # Сумма и размеры строки — как их пишет русский Excel (S3): «6 500,00»,
    # «6 500», «6500,5», «2,0» листа. Было 400 «Требуется численное значение»,
    # хотя та же ячейка во «Вставить из Excel» проходила (D-121).
    NUMERIC = ("width", "height", "length", "sheet_count", "quantity", "cost", "cost_fc")

    material_name = serializers.CharField(source="material.name", read_only=True)
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    # Единица, в которой лежит `quantity`: у площадных — кв.м, у штучных — своя.
    unit = serializers.SerializerMethodField()
    # Код единицы для перевода на фронте (`unit.*`): русская подпись `unit` в
    # кыргызской или английской накладной торчала чужим словом.
    unit_code = serializers.SerializerMethodField()
    # «Вернуть поставщику»: в чём считают возврат (лист, м, шт), сколько ещё на
    # полке и сколько принято в этих единицах.
    return_unit = serializers.SerializerMethodField()
    returnable = serializers.SerializerMethodField()
    return_total = serializers.SerializerMethodField()

    class Meta:
        model = SupplyLine
        fields = [
            "id", "material", "material_name", "form",
            "width", "height", "length", "sheet_count",
            "quantity", "unit", "unit_code", "cost", "cost_fc", "unit_cost", "code",
            # Партия строки — кнопке «Исправить приход» в карточке накладной.
            "roll", "return_unit", "returnable", "return_total",
        ]
        extra_kwargs = {
            "roll": {"read_only": True},
            # У штучного материала количество ВВОДЯТ, у площадного оно считается
            # из размеров и присланное значение игнорируется (см. `line_quantity`).
            "quantity": {"required": False},
            # Сумма строки в сомах обязательна для сомовой накладной (это
            # проверяет накладная целиком: в валюте её считает сервер из
            # `cost_fc` по курсу) и не отрицательна: она идёт в закуп месяца и
            # в себестоимость партии. Ноль допустим явно (подарок поставщика).
            # Строка с минусом — это возврат: у него своя кнопка, и с 10.10 она
            # работает и для накладной закрытого месяца (D-171). Подсказка
            # вместо «больше либо равно 0» (10e перепроверки).
            "cost": {"required": False, "min_value": Decimal("0"),
                     "error_messages": {"min_value": NEGATIVE_LINE}},
            "cost_fc": {"required": False, "allow_null": True, "min_value": Decimal("0"),
                        "error_messages": {"min_value": NEGATIVE_LINE}},
        }

    def _return_info(self, obj):
        # Считаем только для чтения готовой накладной, один раз на строку.
        cache = self.context.setdefault("_return_info", {})
        if obj.pk not in cache:
            from .supplier_returns import returnable

            cache[obj.pk] = returnable(obj) if obj.pk else (None, None, None)
        return cache[obj.pk]

    def get_return_unit(self, obj):
        return self._return_info(obj)[0]

    def get_returnable(self, obj):
        return self._return_info(obj)[1]

    def get_return_total(self, obj):
        return self._return_info(obj)[2]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Закупочные цены — не складовщику (STAFF-07).
        if not _sees_money(self.context):
            data["cost"] = data["cost_fc"] = data["unit_cost"] = None
            data["returnable"] = data["return_total"] = None
        return data

    def get_unit(self, obj):
        return "кв.м" if obj.material.is_roll_material and obj.form != SupplyLine.Form.QTY \
            else obj.material.get_unit_display()

    def get_unit_code(self, obj):
        return "SQM" if obj.material.is_roll_material and obj.form != SupplyLine.Form.QTY \
            else obj.material.unit


class SupplierPaymentSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True, default="")
    supply_number = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default="")
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    settled = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    is_advance = serializers.BooleanField(read_only=True)
    advance_left = serializers.SerializerMethodField()

    class Meta:
        model = SupplierPayment
        fields = [
            "id", "supplier", "supplier_name", "supply", "supply_number", "source", "kind",
            "kind_label", "paid_on", "account", "amount", "settled", "fx_diff", "currency",
            "amount_fc", "rate", "note", "created_by", "created_by_name", "created_at",
            "is_advance", "advance_left",
        ]
        read_only_fields = fields

    def get_supply_number(self, obj):
        if not obj.supply_id:
            return ""
        return obj.supply.number or f"#{obj.supply_id}"

    def get_advance_left(self, obj):
        if not obj.is_advance:
            return None
        from .supplier_ledger import advance_remaining

        return advance_remaining(obj)


class SupplierReturnSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default="")
    lines = serializers.SerializerMethodField()

    class Meta:
        model = SupplierReturn
        fields = [
            "id", "supply", "returned_on", "amount", "amount_fc", "refund", "refund_account",
            "note", "in_place", "created_by_name", "created_at", "lines",
        ]
        read_only_fields = fields

    def get_lines(self, obj):
        return [
            {"material": l.material_id, "label": l.label, "quantity": l.quantity,
             "area": l.area, "cost": l.cost, "cost_fc": l.cost_fc}
            for l in obj.lines.all()
        ]


class SupplierOpeningDebtSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)

    class Meta:
        model = SupplierOpeningDebt
        fields = ["id", "supplier", "supplier_name", "as_of", "amount", "note", "created_at"]
        read_only_fields = ["created_at"]


_MONEY_FIELDS = (
    "stated_total", "paid_amount", "paid_account", "total_cost", "discrepancy", "debt",
    "paid_total", "overpaid", "total_foreign", "paid_foreign", "debt_foreign", "returned_after",
    "paid_cash",
)


class SupplySerializer(_NumbersMixin, serializers.ModelSerializer):
    NUMERIC = ("stated_total", "paid_amount", "rate")

    lines = SupplyLineSerializer(many=True)
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)
    # Реквизиты поставщика нужны печатной форме: лист приёмки без них — просто
    # список позиций, по которому потом не докажешь, от кого он.
    supplier_inn = serializers.CharField(source="supplier.inn", read_only=True)
    supplier_phone = serializers.CharField(source="supplier.phone", read_only=True)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)
    total_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    discrepancy = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    debt = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    paid_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    # Деньгами из кассы (RU-N16) — у валютной накладной не равно «закрыто долга».
    paid_cash = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    overpaid = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_foreign = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    paid_foreign = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    debt_foreign = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    # Возвращено датой возврата (накладная закрытого месяца не переписана, D-171).
    returned_after = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    # Месяц накладной закрыт: возврат пойдёт датой возврата, форма это пишет.
    period_closed = serializers.SerializerMethodField()
    payments = SupplierPaymentSerializer(many=True, read_only=True)
    returns = SupplierReturnSerializer(many=True, read_only=True)
    possible_duplicate_of = serializers.SerializerMethodField()

    class Meta:
        model = Supply
        fields = [
            "id", "number", "supplier", "supplier_name",
            "supplier_inn", "supplier_phone", "received_on",
            "stated_total", "paid_amount", "paid_account", "note", "lines",
            "is_opening", "currency", "rate",
            "total_cost", "discrepancy", "debt", "paid_total", "paid_cash", "overpaid",
            "total_foreign", "paid_foreign", "debt_foreign", "returned_after", "period_closed",
            "payments", "returns", "possible_duplicate_of",
            "created_by", "created_by_name", "created_at",
        ]
        read_only_fields = ["created_by", "created_at"]

    def get_possible_duplicate_of(self, obj):
        return (self.context.get("dupes") or {}).get(obj.id)

    def get_period_closed(self, obj):
        if obj.received_on is None:
            return False
        if "_closed_through" not in self.context:
            from finance.periods import closed_through

            self.context["_closed_through"] = closed_through()
        limit = self.context["_closed_through"]
        return bool(limit and obj.received_on <= limit)

    def validate_currency(self, value):
        code = (value or "KGS").strip().upper()
        if code not in Supply.CURRENCIES:
            raise serializers.ValidationError(
                "Валюта: " + ", ".join(Supply.CURRENCIES) + "."
            )
        return code

    def validate_paid_amount(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Оплата не может быть отрицательной.")
        return value

    def validate(self, attrs):
        inst = self.instance
        if inst is not None:
            # Валюта, курс и режим «начальные остатки» задают цены партий — после
            # проведения не меняются.
            for name in ("currency", "rate", "is_opening"):
                if name in attrs and attrs[name] != getattr(inst, name):
                    raise serializers.ValidationError({
                        name: "После проведения не меняется: отмените накладную и заведите заново."
                    })
            if "paid_amount" in attrs or "paid_account" in attrs:
                amount = attrs.get("paid_amount", inst.paid_amount) or Decimal("0")
                account = attrs.get("paid_account", inst.paid_account)
                if amount > 0 and account not in ("CASH", "BANK"):
                    raise serializers.ValidationError({
                        "paid_account": "Укажите, чем платили: наличными или с банка."
                    })
            return attrs

        currency = attrs.get("currency") or "KGS"
        attrs["currency"] = currency
        if currency == "KGS":
            attrs["rate"] = Decimal("1")
        else:
            rate = attrs.get("rate")
            if rate is None or rate <= 0:
                raise serializers.ValidationError({
                    "rate": f"Накладная в {currency}: укажите курс — сколько сом за единицу валюты."
                })
        paid = attrs.get("paid_amount") or Decimal("0")
        account = attrs.get("paid_account") or ""
        if attrs.get("is_opening"):
            if currency != "KGS":
                raise serializers.ValidationError({"currency": "Начальные остатки вводятся в сомах."})
            if paid > 0 or account:
                raise serializers.ValidationError({
                    "paid_amount": "Начальные остатки — склад на дату переезда: оплаты по ним нет. "
                                   "Долг поставщику вносится отдельно, в его карточке."
                })
        elif paid > 0 and account not in ("CASH", "BANK"):
            # Раньше оплата без счёта молча не попадала в кассу (аудит F1).
            raise serializers.ValidationError({
                "paid_account": "Укажите, чем платили: наличными или с банка. Без счёта "
                                "оплата не попадает в кассу."
            })
        problems, bad = [], False
        for line in attrs.get("lines") or []:
            errs = {}
            if currency == "KGS":
                if line.get("cost") is None:
                    errs["cost"] = ["Укажите сумму строки."]
            else:
                fc = line.get("cost_fc")
                if fc is None:
                    errs["cost_fc"] = [f"Укажите сумму строки в {currency}."]
                else:
                    line["cost"] = (fc * attrs["rate"]).quantize(Decimal("0.01"))
            bad = bad or bool(errs)
            problems.append(errs)
        if bad:
            raise serializers.ValidationError({"lines": problems})
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Деньги накладной и платежей — админу и бухгалтеру; складовщик
        # принимает товар, а почём купили и сколько должны, ему знать незачем.
        if not _sees_money(self.context):
            for name in _MONEY_FIELDS:
                data[name] = None
            data["payments"] = None
            data["returns"] = None
        return data


class WasteLineSerializer(serializers.Serializer):
    """Строка отхода — теми же мерками, что и приход (см. warehouse/waste.py)."""

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    form = serializers.ChoiceField(choices=["SHEET", "AREA", "ROLL", "QTY"], required=False, allow_null=True)
    width = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    height = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    sheet_count = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    area = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True)
    length = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True)
    # Партия/рулон, из которого ушёл брак. Не указана — FIFO, как при продаже.
    roll = serializers.PrimaryKeyRelatedField(queryset=Roll.objects.all(), required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        from .waste import FORM_AREA, FORM_ROLL, FORM_SHEET, line_quantity

        material = attrs["material"]
        roll = attrs.get("roll")
        if roll is not None and roll.material_id != material.id:
            raise serializers.ValidationError(f"«{material.name}»: партия другого материала.")
        form = attrs.get("form")
        if material.sells_by_metre:
            # Рулон: метры (или площадь, которую переведём шириной рулона).
            if form not in (FORM_ROLL, FORM_AREA, None):
                raise serializers.ValidationError(
                    f"«{material.name}» — рулон: отход вводится метрами или площадью."
                )
            value = attrs.get("area") if form == FORM_AREA else attrs.get("length")
            if not value or value <= 0:
                raise serializers.ValidationError(
                    f"«{material.name}»: укажите, сколько метров (или кв.м) ушло в отход."
                )
            return attrs
        if material.is_roll_material and form == FORM_ROLL:
            raise serializers.ValidationError(
                f"«{material.name}» приходит листами — отход считается листами или площадью."
            )
        if material.is_roll_material and form in (None, FORM_SHEET) and not all(
            (attrs.get("width"), attrs.get("height"), attrs.get("sheet_count"))
        ):
            raise serializers.ValidationError(
                f"«{material.name}»: для листа укажите ширину, высоту и количество листов."
            )
        qty = line_quantity(
            material, form or ("SHEET" if material.is_roll_material else "QTY"),
            width=attrs.get("width"), height=attrs.get("height"),
            sheet_count=attrs.get("sheet_count"), area=attrs.get("area"),
            length=attrs.get("length"), quantity=attrs.get("quantity"), roll=roll,
        )
        if qty <= 0:
            raise serializers.ValidationError(
                f"«{material.name}»: не из чего посчитать количество — проверьте размеры или количество."
            )
        from .rolls import snap_tail

        if snap_tail(material, qty) > material.quantity:
            unit = "кв.м" if material.is_roll_material else material.get_unit_display()
            raise serializers.ValidationError(
                f"«{material.name}»: в отход {qty.normalize():f} {unit}, а на складе "
                f"{material.quantity.normalize():f} {unit} — столько списать нельзя."
            )
        return attrs


class WasteSerializer(serializers.Serializer):
    """POST /warehouse/waste/ — отход (брак) одним действием, несколькими строками."""

    happened_on = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)
    lines = WasteLineSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Добавьте хотя бы одну строку отхода.")
        return value

    def validate_happened_on(self, value):
        return check_op_day(value, "Дата отхода")


class LotCorrectionSerializer(serializers.Serializer):
    """«Исправить приход»: что правим и на что (см. warehouse/lot_correction.py).

    Партия (`roll`) или строка накладной без партии (`supply_line`, штучный
    материал). Поля, которых нет в запросе, остаются как были; сумма не
    указана — цена единицы та же, сумма идёт за количеством.
    """

    roll = serializers.PrimaryKeyRelatedField(queryset=Roll.objects.all(), required=False, allow_null=True)
    supply_line = serializers.PrimaryKeyRelatedField(
        queryset=SupplyLine.objects.all(), required=False, allow_null=True,
    )
    purchase_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True,
    )
    width = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    height = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    length = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    sheet_count = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True,
    )
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True,
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        if bool(attrs.get("roll")) == bool(attrs.get("supply_line")):
            raise serializers.ValidationError("Укажите партию или строку накладной.")
        return attrs


class StockTransferInputSerializer(_NumbersMixin, serializers.Serializer):
    """Перемещение между площадками (STK-05/G4-N4): сколько — в единицах
    хранения (`quantity`), листами (`sheets`) или метрами с рулона (`metres`
    + `roll`)."""

    NUMERIC = ("quantity", "sheets", "metres")

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    from_site = serializers.PrimaryKeyRelatedField(
        queryset=ProductionSite.objects.all(), required=False, allow_null=True)
    to_site = serializers.PrimaryKeyRelatedField(
        queryset=ProductionSite.objects.all(), required=False, allow_null=True)
    roll = serializers.PrimaryKeyRelatedField(queryset=Roll.objects.all(), required=False, allow_null=True)
    quantity = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False, allow_null=True)
    sheets = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True)
    metres = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0, required=False, allow_null=True)
    happened_on = serializers.DateField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate_happened_on(self, value):
        return check_op_day(value, "Дата перемещения")

    def validate(self, attrs):
        material, roll = attrs["material"], attrs.get("roll")
        if roll is not None and roll.material_id != material.id:
            raise serializers.ValidationError({"roll": "Партия другого материала."})
        given = [k for k in ("quantity", "sheets", "metres") if attrs.get(k) is not None]
        if len(given) != 1:
            raise serializers.ValidationError({"quantity": "Укажите, сколько перемещаете: количество, листы или метры."})
        if attrs.get("sheets") is not None:
            attrs["quantity"] = _sheets_to_area(material, attrs["sheets"], "sheets")
        elif attrs.get("metres") is not None:
            if roll is None or not roll.width:
                raise serializers.ValidationError({"roll": "Метрами — с конкретного рулона: выберите рулон."})
            attrs["quantity"] = (attrs["metres"] * roll.width).quantize(Decimal("0.0001"))
        if attrs["quantity"] <= 0:
            raise serializers.ValidationError({"quantity": "Количество должно быть больше нуля."})
        if attrs.get("from_site") == attrs.get("to_site"):
            raise serializers.ValidationError({"to_site": "Откуда и куда — одна и та же площадка."})
        return attrs


class StockTransferSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    from_site_name = serializers.CharField(source="from_site.name", read_only=True, default=None)
    to_site_name = serializers.CharField(source="to_site.name", read_only=True, default=None)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default=None)
    cost = serializers.SerializerMethodField()

    def get_cost(self, obj):
        return obj.cost if _sees_money(self.context) else None

    class Meta:
        from .models import StockTransfer

        model = StockTransfer
        fields = [
            "id", "material", "material_name", "from_site", "from_site_name", "to_site",
            "to_site_name", "area", "cost", "happened_on", "note", "created_by_name", "created_at",
        ]
