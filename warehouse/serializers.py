from decimal import Decimal

from rest_framework import serializers

from .models import (
    InventoryLog,
    Material,
    MaterialImage,
    MaterialMonthOpening,
    MaterialType,
    ProductionSite,
    Roll,
    Supplier,
    Supply,
    SupplyLine,
)


class MaterialImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaterialImage
        fields = ["id", "material", "image", "is_primary", "uploaded_at"]
        read_only_fields = ["uploaded_at"]


class MaterialSerializer(serializers.ModelSerializer):
    images = MaterialImageSerializer(many=True, read_only=True)
    primary_image = serializers.SerializerMethodField()
    is_below_critical = serializers.BooleanField(read_only=True)
    sqm_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    sheets_remaining = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, allow_null=True
    )
    stock_value = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    type_name = serializers.CharField(source="type.name", read_only=True)
    production_name = serializers.CharField(source="production.name", read_only=True)
    # Подсказка для формы: как назвался бы материал по заполненным полям.
    suggested_name = serializers.CharField(read_only=True)

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
            "cut_rate_per_pm",
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

    def get_primary_image(self, obj):
        request = self.context.get("request")
        primary = next((img for img in obj.images.all() if img.is_primary), None)
        primary = primary or obj.images.first()
        if not primary:
            return None
        url = primary.image.url
        return request.build_absolute_uri(url) if request else url


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
            "critical_balance", "purchase_price", "price_per_unit",
            "price_per_sqm", "piece_price", "cut_rate_per_pm",
            "wholesale_price", "wholesale_min_qty", "production",
        ]

    def validate(self, attrs):
        # Задан размер листа — значит материал листовой: учёт в кв.м, приход
        # партиями, продажа и листом, и площадью. Отдельной галкой этот выбор
        # ничего не добавляет, система его уже знает. Явно переданное значение
        # (ячейка «Учёт» в сетке) побеждает расчёт.
        has_sheet = attrs.get("sheet_width") and attrs.get("sheet_height")
        if has_sheet and "is_roll_material" not in self.initial_data:
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

    class Meta:
        model = InventoryLog
        fields = [
            "id",
            "type",
            "type_display",
            "material",
            "material_name",
            "material_unit",
            "quantity_changed",
            "actual_price",
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


class AdjustmentSerializer(serializers.Serializer):
    """Inventory adjustment — reconcile actual vs system stock."""

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    counted_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0
    )
    reason = serializers.CharField(required=False, allow_blank=True)


class WriteOffSerializer(serializers.Serializer):
    """Write off stock for damage / defect / loss / expiry."""

    REASONS = {
        "DAMAGE": "Порча",
        "DEFECT": "Брак",
        "LOSS": "Утеря",
        "EXPIRY": "Истёк срок",
        "OTHER": "Прочее",
    }

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0,
        help_text="Списываемое количество (положительное число)",
    )
    reason_code = serializers.ChoiceField(choices=list(REASONS.keys()))
    note = serializers.CharField(required=False, allow_blank=True)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Количество должно быть больше нуля.")
        return value

    def validate(self, attrs):
        material = attrs["material"]
        if attrs["quantity"] > material.quantity:
            raise serializers.ValidationError(
                {"quantity": f"Нельзя списать больше, чем на складе ({material.quantity})."}
            )
        return attrs

    def reason_text(self) -> str:
        label = self.REASONS[self.validated_data["reason_code"]]
        note = self.validated_data.get("note")
        return f"Списание: {label}." + (f" {note}" if note else "")


class RollSerializer(serializers.ModelSerializer):
    cost_per_sqm = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    material_name = serializers.CharField(source="material.name", read_only=True)
    dimensions_label = serializers.CharField(read_only=True)

    class Meta:
        model = Roll
        fields = [
            "id",
            "material",
            "material_name",
            "code",
            "form",
            "width",
            "length",
            "height",
            "sheet_count",
            "dimensions_label",
            "initial_area",
            "remaining_area",
            "purchase_cost",
            "cost_per_sqm",
            "received_at",
        ]


class RollIntakeSerializer(serializers.Serializer):
    """Receive a lot (roll or sheets) for an area-material.

    ROLL  → width × length = area.
    SHEET → width × height × sheet_count = area.
    """

    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    code = serializers.CharField(required=False, allow_blank=True)
    form = serializers.ChoiceField(choices=Roll.Form.values)
    width = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    length = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    height = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True)
    sheet_count = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    purchase_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    # Дата поступления партии — по ней же идёт FIFO.
    received_on = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["form"] == Roll.Form.ROLL:
            if not attrs.get("width") or not attrs.get("length"):
                raise serializers.ValidationError("Для рулона укажите ширину и длину.")
        else:  # SHEET
            if not attrs.get("width") or not attrs.get("height") or not attrs.get("sheet_count"):
                raise serializers.ValidationError(
                    "Для листа укажите ширину, высоту и количество листов."
                )
        return attrs


class MaterialMonthOpeningSerializer(serializers.ModelSerializer):
    """Остаток материала на начало месяца — ручной ввод, как в Excel."""

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
    новую фирму заводит складовщик прямо в накладной."""

    supplies_count = serializers.SerializerMethodField()
    debt = serializers.SerializerMethodField()

    class Meta:
        model = Supplier
        fields = [
            "id", "name", "phone", "inn", "note", "is_archived",
            "supplies_count", "debt",
        ]

    def get_supplies_count(self, obj) -> int:
        return obj.supplies.count()

    def get_debt(self, obj):
        """Сколько мы должны этому поставщику по всем его накладным."""
        return sum((s.debt for s in obj.supplies.prefetch_related("lines")), Decimal("0"))


class SupplyLineSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    # Единица, в которой лежит `quantity`: у площадных — кв.м, у штучных — своя.
    unit = serializers.SerializerMethodField()

    class Meta:
        model = SupplyLine
        fields = [
            "id", "material", "material_name", "form",
            "width", "height", "length", "sheet_count",
            "quantity", "unit", "cost", "unit_cost", "code",
        ]
        extra_kwargs = {
            # У штучного материала количество ВВОДЯТ, у площадного оно считается
            # из размеров и присланное значение игнорируется (см. `line_quantity`).
            "quantity": {"required": False},
        }

    def get_unit(self, obj):
        return "кв.м" if obj.material.is_roll_material and obj.form != SupplyLine.Form.QTY \
            else obj.material.get_unit_display()


class SupplySerializer(serializers.ModelSerializer):
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

    class Meta:
        model = Supply
        fields = [
            "id", "number", "supplier", "supplier_name",
            "supplier_inn", "supplier_phone", "received_on",
            "stated_total", "paid_amount", "note", "lines",
            "total_cost", "discrepancy", "debt",
            "created_by", "created_by_name", "created_at",
        ]
        read_only_fields = ["created_by", "created_at"]
