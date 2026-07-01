from decimal import Decimal

from django.db.models import Count, Sum
from rest_framework import serializers

from .models import Client, ReferralChangeRequest


def client_ltv(client) -> Decimal:
    agg = client.receipts.aggregate(gross=Sum("total_price"), refunded=Sum("refunded_amount"))
    return (agg["gross"] or Decimal("0")) - (agg["refunded"] or Decimal("0"))


def client_debt(client) -> Decimal:
    """Сколько клиент должен = Σ долга по его чекам (Receipt.debt уже учитывает
    отмену/оплату/возвраты). Использует prefetch'нутые receipts — без доп. запросов."""
    return sum((r.debt for r in client.receipts.all()), Decimal("0"))


class ClientSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)
    is_telegram_linked = serializers.BooleanField(read_only=True)
    has_password = serializers.BooleanField(read_only=True)
    referred_by_name = serializers.CharField(source="referred_by.display_name", read_only=True)
    referrals_count = serializers.SerializerMethodField()
    debt = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            "id",
            "type",
            "full_name",
            "company_name",
            "phone",
            "telegram_chat_id",
            "display_name",
            "is_telegram_linked",
            "has_password",
            "referred_by",
            "referred_by_name",
            "referrals_count",
            "debt",
            "created_at",
        ]
        read_only_fields = ["telegram_chat_id", "created_at"]

    def get_referrals_count(self, obj):
        return obj.referrals.count()

    def get_debt(self, obj):
        return client_debt(obj)

    def validate_referred_by(self, value):
        if value and self.instance and value.pk == self.instance.pk:
            raise serializers.ValidationError("Клиент не может привести сам себя.")
        # Referral is locked once set. Storekeepers cannot change or clear it —
        # they must file a ReferralChangeRequest for an admin to approve. Admins
        # are the approval authority, so they may override it directly.
        if self.instance and self.instance.referred_by_id is not None:
            if not value or value.pk != self.instance.referred_by_id:
                request = self.context.get("request")
                is_admin = bool(
                    request and getattr(request.user, "is_admin_role", False)
                )
                if not is_admin:
                    raise serializers.ValidationError(
                        "Реферал зафиксирован. Изменить его может только администратор "
                        "— подайте заявку на смену."
                    )
        return value

    def validate(self, attrs):
        ctype = attrs.get("type", getattr(self.instance, "type", None))
        if ctype == Client.Type.OSOO and not attrs.get(
            "company_name", getattr(self.instance, "company_name", None)
        ):
            raise serializers.ValidationError(
                {"company_name": "Для ОСОО укажите название компании."}
            )
        if ctype == Client.Type.PHYSICAL and not attrs.get(
            "full_name", getattr(self.instance, "full_name", None)
        ):
            raise serializers.ValidationError(
                {"full_name": "Для физ. лица укажите ФИО."}
            )
        return attrs


class ReferralChangeRequestSerializer(serializers.ModelSerializer):
    """Read-only view of a referral-change request for the moderation queue."""

    client_name = serializers.CharField(source="client.display_name", read_only=True)
    new_referred_by_name = serializers.CharField(
        source="new_referred_by.display_name", read_only=True, default=None
    )
    previous_referred_by_name = serializers.CharField(
        source="previous_referred_by.display_name", read_only=True, default=None
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    requested_by_name = serializers.CharField(
        source="requested_by.username", read_only=True, default=None
    )
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.username", read_only=True, default=None
    )

    class Meta:
        model = ReferralChangeRequest
        fields = [
            "id",
            "client",
            "client_name",
            "new_referred_by",
            "new_referred_by_name",
            "previous_referred_by",
            "previous_referred_by_name",
            "status",
            "status_display",
            "requested_by",
            "requested_by_name",
            "reviewed_by_name",
            "reviewed_at",
            "reason",
            "created_at",
        ]


class ClientDetailSerializer(ClientSerializer):
    """Includes purchase statistics / LTV and the referral chain for CRM."""

    stats = serializers.SerializerMethodField()
    referrals = serializers.SerializerMethodField()
    pending_referral_request = serializers.SerializerMethodField()

    class Meta(ClientSerializer.Meta):
        fields = ClientSerializer.Meta.fields + [
            "stats",
            "referrals",
            "pending_referral_request",
        ]

    def get_pending_referral_request(self, obj):
        req = obj.referral_requests.filter(
            status=ReferralChangeRequest.Status.PENDING
        ).first()
        return ReferralChangeRequestSerializer(req).data if req else None

    def get_stats(self, obj):
        receipts = obj.receipts.all()
        agg = receipts.aggregate(
            orders=Count("id"),
            gross=Sum("total_price"),
            refunded=Sum("refunded_amount"),
        )
        gross = agg["gross"] or Decimal("0")
        refunded = agg["refunded"] or Decimal("0")
        return {
            "orders_count": agg["orders"] or 0,
            "lifetime_value": gross - refunded,
            "gross": gross,
            "refunded": refunded,
        }

    def get_referrals(self, obj):
        items = []
        total = Decimal("0")
        for ref in obj.referrals.all():
            ltv = client_ltv(ref)
            total += ltv
            items.append({"id": ref.id, "display_name": ref.display_name, "lifetime_value": ltv})
        return {"count": len(items), "total_value": total, "list": items}
