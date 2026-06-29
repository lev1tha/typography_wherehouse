from decimal import Decimal

from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin
from sales.models import Receipt, TransactionItem
from services.models import PricingSettings
from warehouse.models import InventoryLog, Material

from .models import AuditLog
from .serializers import AuditLogSerializer

_ZERO = Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField())
# Line revenue = price_per_item × quantity (used for work/material splits).
_LINE_SUM = Coalesce(
    Sum(F("price_per_item") * F("quantity")), Decimal("0"), output_field=DecimalField()
)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Admin-only hidden trail of staff actions."""

    queryset = AuditLog.objects.select_related("user").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdmin]
    filterset_fields = ["user"]
    search_fields = ["action"]
    ordering = ["-created_at"]


class DashboardView(APIView):
    """GET /api/audit/dashboard/ — admin financial summary & analytics."""

    permission_classes = [IsAdmin]

    def get(self, request):
        paid = Receipt.objects.filter(payment_status=Receipt.PaymentStatus.PAID)

        # Unrealised asset: sum(quantity * purchase_price) over all materials.
        stock_value = sum(
            (m.quantity * m.purchase_price for m in Material.objects.all()),
            Decimal("0"),
        )

        revenue_cash = paid.filter(payment_method=Receipt.PaymentMethod.CASH).aggregate(
            v=_ZERO
        )["v"]
        revenue_online = paid.filter(
            payment_method=Receipt.PaymentMethod.ONLINE
        ).aggregate(v=_ZERO)["v"]

        # Revenue split — work (services) vs material — over paid, non-returned lines.
        paid_lines = TransactionItem.objects.filter(
            receipt__payment_status=Receipt.PaymentStatus.PAID, is_returned=False
        )
        work_revenue = paid_lines.filter(type=TransactionItem.Type.SERVICE).aggregate(
            v=_LINE_SUM
        )["v"]
        material_revenue = paid_lines.filter(
            type=TransactionItem.Type.MATERIAL
        ).aggregate(v=_LINE_SUM)["v"]
        commission = PricingSettings.load().master_commission_percent
        master_wage = (work_revenue * commission / Decimal("100")).quantize(Decimal("0.01"))

        service_items = TransactionItem.objects.filter(
            type=TransactionItem.Type.SERVICE, is_returned=False
        )
        services_count = service_items.count()

        # Material consumed by services, via technological cards.
        materials_consumed = Decimal("0")
        for item in service_items.select_related("service").prefetch_related(
            "service__recipes"
        ):
            for recipe in item.service.recipes.all():
                materials_consumed += recipe.consumption_per_unit * item.quantity

        refunded_total = Receipt.objects.aggregate(
            v=Coalesce(Sum("refunded_amount"), Decimal("0"), output_field=DecimalField())
        )["v"]

        # Material lost via write-offs and negative inventory adjustments.
        lost_qty = (
            InventoryLog.objects.filter(
                type__in=[InventoryLog.Type.ADJUSTMENT, InventoryLog.Type.WRITE_OFF],
                quantity_changed__lt=0,
            ).aggregate(
                v=Coalesce(
                    Sum("quantity_changed"), Decimal("0"), output_field=DecimalField()
                )
            )["v"]
        )

        return Response(
            {
                "unrealised_asset": stock_value,
                "revenue": {
                    "cash": revenue_cash,
                    "online": revenue_online,
                    "total": revenue_cash + revenue_online,
                },
                "breakdown": {
                    "work_revenue": work_revenue,
                    "material_revenue": material_revenue,
                    "master_wage": master_wage,
                    "commission_percent": commission,
                },
                "services_performed": services_count,
                "materials_consumed_by_services": materials_consumed,
                "refunds": {
                    "total_refunded": refunded_total,
                    "material_lost_quantity": abs(lost_qty),
                },
                "low_stock_count": sum(
                    1 for m in Material.objects.all() if m.is_below_critical
                ),
            }
        )


class ClientPurchasesView(APIView):
    """GET /api/audit/client-purchases/ — per-client material purchase analytics.

    Admin-only. Aggregates paid, non-returned MATERIAL lines per client:
    total material spend, total area/qty, order count. Sortable via ?ordering=.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        ordering = request.query_params.get("ordering", "-material_spend")
        allowed = {
            "material_spend", "-material_spend",
            "material_qty", "-material_qty",
            "orders", "-orders",
            "client_name", "-client_name",
        }
        if ordering not in allowed:
            ordering = "-material_spend"

        rows = (
            TransactionItem.objects.filter(
                type=TransactionItem.Type.MATERIAL,
                is_returned=False,
                receipt__payment_status=Receipt.PaymentStatus.PAID,
                receipt__client__isnull=False,
            )
            .values("receipt__client")
            .annotate(
                material_spend=_LINE_SUM,
                material_qty=Coalesce(
                    Sum("quantity"), Decimal("0"), output_field=DecimalField()
                ),
            )
        )

        # Attach client display data + order count, then sort in Python (small set).
        from clients.models import Client

        client_ids = [r["receipt__client"] for r in rows]
        clients = {c.id: c for c in Client.objects.filter(id__in=client_ids)}
        result = []
        for r in rows:
            client = clients.get(r["receipt__client"])
            if not client:
                continue
            orders = (
                Receipt.objects.filter(
                    client=client, payment_status=Receipt.PaymentStatus.PAID
                ).count()
            )
            result.append({
                "client_id": client.id,
                "client_name": client.display_name,
                "phone": client.phone,
                "material_spend": r["material_spend"],
                "material_qty": r["material_qty"],
                "orders": orders,
            })

        reverse = ordering.startswith("-")
        key = ordering.lstrip("-")
        result.sort(key=lambda x: x[key] if key != "client_name" else x[key].lower(), reverse=reverse)
        return Response(result)
