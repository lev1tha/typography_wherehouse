from rest_framework import serializers

from .models import (
    InventoryLog,
    Material,
    MaterialImage,
    MaterialMonthOpening,
    MaterialType,
    ProductionSite,
    Roll,
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


class SupplySerializer(serializers.Serializer):
    """Storekeeper receives a new supply batch (increments stock)."""

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
