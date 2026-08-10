from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import serializers

from .models import Client, ReferralChangeRequest


def client_ltv(client) -> Decimal:
    agg = client.receipts.aggregate(gross=Sum("total_price"), refunded=Sum("refunded_amount"))
    return (agg["gross"] or Decimal("0")) - (agg["refunded"] or Decimal("0"))


def client_debt(client) -> Decimal:
    """Сколько клиент должен = Σ долга по его чекам (Receipt.debt уже учитывает
    отмену/оплату/возвраты). Использует prefetch'нутые receipts — без доп. запросов."""
    return sum((r.debt for r in client.receipts.all()), Decimal("0"))


def client_change_due(client) -> Decimal:
    """Сколько ЦЕХ должен клиенту сдачей — зеркало долга.

    Клиент принёс больше, чем стоил заказ, а мелочи в кассе не было. Пока сдачу
    не отдали, эти деньги его, и на вопрос «сколько за нами осталось» отвечать
    должна система, а не память кассира.
    """
    return sum((r.change_due for r in client.receipts.all()), Decimal("0"))


class ClientSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)
    is_telegram_linked = serializers.BooleanField(read_only=True)
    has_password = serializers.BooleanField(read_only=True)
    referred_by_name = serializers.CharField(source="referred_by.display_name", read_only=True)
    referrals_count = serializers.SerializerMethodField()
    debt = serializers.SerializerMethodField()
    change_due = serializers.SerializerMethodField()
    orders_count = serializers.SerializerMethodField()

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
            "change_due",
            "orders_count",
            "created_at",
        ]
        read_only_fields = ["telegram_chat_id", "created_at"]

    def get_referrals_count(self, obj):
        return obj.referrals.count()

    def get_debt(self, obj):
        # Вьюсет считает долг аннотацией (её же использует сортировка по клику).
        # Fallback на Python нужен там, где клиент пришёл без аннотации —
        # например из вложенных сериализаторов.
        annotated = getattr(obj, "debt", None)
        return annotated if annotated is not None else client_debt(obj)

    def get_change_due(self, obj):
        annotated = getattr(obj, "change_due_total", None)
        return annotated if annotated is not None else client_change_due(obj)

    def get_orders_count(self, obj):
        annotated = getattr(obj, "orders_count", None)
        if annotated is not None:
            return annotated
        return obj.receipts.exclude(status="CANCELLED").count()

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

    def validate_phone(self, value):
        """Тот же номер в другом написании — тот же клиент, а не новый.

        Уникальность на поле проверяет СТРОКУ, поэтому `0555 111 222` спокойно
        заводился поверх `+996555111222`, и один человек оказывался в списке
        дважды. Ловим это по цифрам и говорим, под кем номер уже записан.
        """
        from .phones import find_client_by_phone

        twin = find_client_by_phone(value)
        if twin and (self.instance is None or twin.pk != self.instance.pk):
            raise serializers.ValidationError(
                f"Этот номер уже записан за клиентом «{twin.display_name}» ({twin.phone})."
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
    orders = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()
    pending_referral_request = serializers.SerializerMethodField()

    class Meta(ClientSerializer.Meta):
        fields = ClientSerializer.Meta.fields + [
            "stats",
            "referrals",
            "orders",
            "payments",
            "pending_referral_request",
        ]

    def get_payments(self, obj):
        """История оплат клиента: когда и сколько он реально принёс.

        По полю «оплачено» на чеке этого не видно — особенно после общей выплаты
        (одна сумма разошлась по нескольким заказам) и оплат задним числом.
        Период фильтрует по ДАТЕ ОПЛАТЫ, а не по дате заказа.
        """
        from sales.models import Payment

        d_from = self.context.get("date_from")
        d_to = self.context.get("date_to")
        qs = Payment.objects.filter(receipt__client=obj).select_related("receipt")
        if d_from:
            qs = qs.filter(paid_on__gte=d_from)
        if d_to:
            qs = qs.filter(paid_on__lte=d_to)
        return [
            {
                "id": p.id,
                "amount": p.amount,
                "method": p.method,
                "method_display": p.get_method_display(),
                "paid_on": p.paid_on,
                "order_number": p.receipt.order_number,
                "order_title": p.receipt.title,
            }
            # Хвост истории обрезаем: у постоянного клиента это сотни строк,
            # а карточка показывает последние оплаты.
            for p in qs[:50]
        ]

    def get_orders(self, obj):
        """Заказы клиента (что он покупал) — для карточки CRM: номер, дата, сумма,
        статусы, долг и позиции. Receipt.Meta уже сортирует по -created_at.

        Если в списке выбран период, карточка показывает заказы того же периода —
        иначе фильтр «июль» открывал бы карточку со всей историей за год."""
        d_from = self.context.get("date_from")
        d_to = self.context.get("date_to")
        rows = []
        for r in obj.receipts.all():
            created = timezone.localtime(r.created_at).date()
            if (d_from and created < d_from) or (d_to and created > d_to):
                continue
            items = [
                {
                    "title": (
                        i.material.name if i.material_id
                        else (i.service.name if i.service_id else "—")
                    ),
                    "quantity": i.quantity,
                    "line_total": i.line_total,
                }
                for i in r.items.all()
                if not i.is_returned
            ]
            rows.append({
                "id": r.id,
                "order_number": r.order_number,
                "title": r.title,
                "created_at": r.created_at,
                "total_price": r.total_price,
                "payment_status": r.payment_status,
                "fulfillment_status": r.fulfillment_status,
                "debt": r.debt,
                "change_due": r.change_due,
                "items": items,
            })
        return rows

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
        # Реферальный бонус — фикс. сумма за каждого приведённого клиента
        # (редактируется в Финансах). Считается справочно и только показывается —
        # в расходы автоматически не списывается (решение заказчика).
        from finance.models import FinanceSettings

        rate = FinanceSettings.load().referral_bonus
        items = []
        total = Decimal("0")
        for ref in obj.referrals.all():
            ltv = client_ltv(ref)
            total += ltv
            items.append({"id": ref.id, "display_name": ref.display_name, "lifetime_value": ltv})
        count = len(items)
        return {
            "count": count,
            "total_value": total,
            "bonus": rate * count,
            "list": items,
        }
