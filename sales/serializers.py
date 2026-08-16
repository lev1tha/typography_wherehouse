import base64
import io

import qrcode
from rest_framework import serializers

from clients.models import Client
from services.models import PrintingService
from warehouse.models import Material

from .models import Receipt, TransactionItem


def _qr_data_uri(text: str) -> str:
    """Render `text` as a base64 PNG data URI for inline <img> display."""
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _is_admin(context) -> bool:
    """Показывать ли закупочные цифры. Себестоимость и маржа — владельцу и
    бухгалтеру: складовщик оформляет и выдаёт заказы, но почём цех купил
    материал, ему знать незачем (те же правила, что у финансовых разделов).
    Бухгалтеру наоборот: эти цифры и есть его работа."""
    request = context.get("request")
    return bool(request and getattr(request.user, "sees_money", False))


class TransactionItemSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)
    line_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    cost_total = serializers.SerializerMethodField()
    # Единица измерения строки — для печатных форм: в накладной и счёте колонка
    # «Ед.» обязательна, а вывести её на фронте не из чего: у резки количество
    # в погонных метрах, у листа — в штуках, у куска — в квадратных.
    unit_label = serializers.SerializerMethodField()
    # Код единицы — для перевода на фронте (`unit.*` в словарях): русская
    # подпись `unit_label` в кыргызском или английском документе торчала
    # чужим словом посреди переведённой таблицы.
    unit_code = serializers.SerializerMethodField()

    class Meta:
        model = TransactionItem
        fields = [
            "id",
            "type",
            "material",
            "material_name",
            "service",
            "service_name",
            "quantity",
            "price_per_item",
            "line_total",
            "cost_total",
            "unit_label",
            "unit_code",
            "sale_mode",
            "width",
            "length",
            "is_returned",
        ]

    def get_cost_total(self, obj):
        return obj.cost_total if _is_admin(self.context) else None

    def get_unit_code(self, obj):
        if obj.type == TransactionItem.Type.MATERIAL:
            if obj.sale_mode == TransactionItem.SaleMode.PIECE:
                return "PIECE"
            if obj.material_id and obj.material.is_roll_material:
                return "SQM"
            return obj.material.unit if obj.material_id else "PIECE"
        if obj.service_id and obj.service.uses_running_meter:
            return "METER"
        return "PIECE"

    def get_unit_label(self, obj):
        if obj.type == TransactionItem.Type.MATERIAL:
            if obj.sale_mode == TransactionItem.SaleMode.PIECE:
                return "шт"
            if obj.material_id and obj.material.is_roll_material:
                return "кв.м"
            return obj.material.get_unit_display() if obj.material_id else "шт"
        # Работа резки считается погонными метрами, остальные услуги — штуками
        # (буквы наружной установки) или разом за заказ.
        if obj.service_id and obj.service.uses_running_meter:
            return "пог.м"
        return "шт"


class ReceiptSerializer(serializers.ModelSerializer):
    items = TransactionItemSerializer(many=True, read_only=True)
    client_name = serializers.CharField(source="client.display_name", read_only=True)
    cashier_name = serializers.CharField(source="cashier.username", read_only=True)
    cashier_role = serializers.CharField(source="cashier.get_role_display", read_only=True)
    payment_method_display = serializers.CharField(source="get_payment_method_display", read_only=True)
    has_service = serializers.BooleanField(read_only=True)
    debt = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    payment_qr = serializers.SerializerMethodField()
    # Себестоимость проданного по этому заказу и что от него осталось. Снимок
    # закупки на момент продажи — переоценка склада прошлые заказы не двигает.
    cost_total = serializers.SerializerMethodField()
    margin = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()

    class Meta:
        model = Receipt
        fields = [
            "id",
            "order_number",
            "title",
            "client",
            "client_name",
            "cashier",
            "cashier_name",
            "cashier_role",
            "payment_method",
            "payment_method_display",
            "payment_status",
            "status",
            "fulfillment_status",
            "has_service",
            "total_price",
            "refunded_amount",
            "amount_paid",
            "debt",
            # Сдача, которую клиенту ещё не отдали — долг цеха перед ним.
            "change_due",
            "payment_reference",
            "payment_url",
            "payment_qr",
            "cost_total",
            "margin",
            "items",
            "payments",
            "created_at",
            "updated_at",
        ]

    def get_payment_qr(self, obj):
        # Only render a QR while an online payment is still awaiting settlement.
        if obj.payment_url and obj.payment_status == Receipt.PaymentStatus.PENDING:
            return _qr_data_uri(obj.payment_url)
        return None

    def get_cost_total(self, obj):
        return obj.cost_total if _is_admin(self.context) else None

    def get_margin(self, obj):
        return obj.margin if _is_admin(self.context) else None

    def get_payments(self, obj):
        """Принятые оплаты по заказу: когда и сколько. Дата может быть задним
        числом — общая выплата по клиенту проводится позже, чем берут деньги."""
        return [
            {
                "id": p.id,
                "amount": p.amount,
                "method": p.method,
                "paid_on": p.paid_on,
            }
            for p in obj.payments.all()
        ]


class SaleItemInputSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=TransactionItem.Type.choices)
    material = serializers.PrimaryKeyRelatedField(
        queryset=Material.objects.all(), required=False, allow_null=True
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=PrintingService.objects.all(), required=False, allow_null=True
    )
    # Три знака, как у колонки `TransactionItem.quantity`: продажа по площади
    # шлёт сюда площадь куска (0.554 кв.м), и с двумя знаками такой заказ
    # отклонялся «не более 2 цифры после запятой».
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=0, required=False, default=0
    )
    # Material sale mode: PIECE = whole sheet/roll at piece_price; SQM = by area.
    mode = serializers.ChoiceField(
        choices=["PIECE", "SQM"], required=False, allow_null=True
    )
    # Cutting / area-service: dimensions (width × length = area).
    width = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    length = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # Cutting only: length of the cut in running metres (drives the work price).
    running_meters = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # Optional manual price overrides (default to the material's catalogue prices).
    material_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    cut_rate = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )

    def validate(self, attrs):
        if attrs["type"] == TransactionItem.Type.MATERIAL and not attrs.get("material"):
            raise serializers.ValidationError("Для позиции материала укажите material.")
        if attrs["type"] == TransactionItem.Type.SERVICE and not attrs.get("service"):
            raise serializers.ValidationError("Для позиции услуги укажите service.")
        return attrs


class SaleCreateSerializer(serializers.Serializer):
    """Checkout payload. `client_id` for an existing client, or `client` dict
    to create one inline (live phone search drives the choice on the frontend).
    """

    client_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), required=False, allow_null=True
    )
    client = serializers.DictField(required=False)
    payment_method = serializers.ChoiceField(choices=Receipt.PaymentMethod.choices)
    # Необязательное название заказа — показывается в списке чеков.
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    amount_paid = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # «Вся сумма»: клиент платит ровно столько, сколько выйдет. Сумму чека знает
    # только сервер после сборки строк, кассе её угадывать нельзя — она считала
    # 978 там, где сервер насчитал 979, и полностью оплаченный заказ повисал с
    # долгом в сом. Флаг важнее `amount_paid`.
    pay_full = serializers.BooleanField(required=False, default=False)
    # Дата заказа задним числом. Не указана — «сейчас». Право проверяет вьюха:
    # по этой дате считается вся отчётность, ставить её в прошлое может админ.
    order_date = serializers.DateField(required=False, allow_null=True)
    items = SaleItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Добавьте хотя бы одну позицию.")
        return value

    def validate_order_date(self, value):
        from django.utils import timezone

        # Будущим числом заказ не оформляют — этой работы ещё не было.
        if value and value > timezone.localdate():
            raise serializers.ValidationError("Дата заказа не может быть в будущем.")
        return value


class RefundSerializer(serializers.Serializer):
    item_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True
    )
