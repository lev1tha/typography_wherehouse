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


class TransactionItemSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)
    line_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

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
            "sale_mode",
            "width",
            "length",
            "is_returned",
        ]


class ReceiptSerializer(serializers.ModelSerializer):
    items = TransactionItemSerializer(many=True, read_only=True)
    client_name = serializers.CharField(source="client.display_name", read_only=True)
    cashier_name = serializers.CharField(source="cashier.username", read_only=True)
    has_service = serializers.BooleanField(read_only=True)
    debt = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    payment_qr = serializers.SerializerMethodField()

    class Meta:
        model = Receipt
        fields = [
            "id",
            "client",
            "client_name",
            "cashier",
            "cashier_name",
            "payment_method",
            "payment_status",
            "status",
            "fulfillment_status",
            "has_service",
            "total_price",
            "refunded_amount",
            "amount_paid",
            "debt",
            "payment_reference",
            "payment_url",
            "payment_qr",
            "items",
            "created_at",
            "updated_at",
        ]

    def get_payment_qr(self, obj):
        # Only render a QR while an online payment is still awaiting settlement.
        if obj.payment_url and obj.payment_status == Receipt.PaymentStatus.PENDING:
            return _qr_data_uri(obj.payment_url)
        return None


class SaleItemInputSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=TransactionItem.Type.choices)
    material = serializers.PrimaryKeyRelatedField(
        queryset=Material.objects.all(), required=False, allow_null=True
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=PrintingService.objects.all(), required=False, allow_null=True
    )
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, default=0
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
    amount_paid = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    items = SaleItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Добавьте хотя бы одну позицию.")
        return value


class RefundSerializer(serializers.Serializer):
    item_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True
    )
