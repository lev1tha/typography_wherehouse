from decimal import Decimal

from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminOrAccountantRead
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, Material

from .models import AuditLog
from .serializers import AuditLogSerializer

_ZERO = Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField())
# Line revenue = price_per_item × quantity (used for work/material splits).
_LINE_SUM = Coalesce(
    Sum(F("price_per_item") * F("quantity")), Decimal("0"), output_field=DecimalField()
)
# Себестоимость строк — снимок закупки на момент списания со склада.
_COST_SUM = Coalesce(Sum("cost_total"), Decimal("0"), output_field=DecimalField())


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Admin-only hidden trail of staff actions."""

    queryset = AuditLog.objects.select_related("user").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_fields = ["user"]
    search_fields = ["action"]
    ordering = ["-created_at"]


class DashboardView(APIView):
    """GET /api/audit/dashboard/ — admin financial summary & analytics."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        # Опциональный период фильтрует денежные показатели по дате чека;
        # складские (актив, материалы на исходе) — всегда «на сейчас».
        date_from = request.query_params.get("date_from") or None
        date_to = request.query_params.get("date_to") or None

        def by_period(qs, field="created_at"):
            if date_from:
                qs = qs.filter(**{f"{field}__date__gte": date_from})
            if date_to:
                qs = qs.filter(**{f"{field}__date__lte": date_to})
            return qs

        # Выручка считается по ВСЕМ заказам периода, кроме отменённых, — по дате
        # заказа. Раньше сюда попадали только полностью оплаченные чеки, и заказ
        # в долг не считался выручкой вообще: материал со склада ушёл, работа
        # сделана, а в отчёте её нет. Отдельно от этого «Финансы» показывают,
        # сколько из выручки уже получено на руки, а сколько ещё в долгу.
        paid = by_period(Receipt.objects.exclude(status=Receipt.Status.CANCELLED))

        # Стоимость склада — по остаткам ПАРТИЙ, у каждой своя себестоимость
        # (Material.stock_value). Раньше здесь стояло quantity × purchase_price,
        # то есть весь остаток оценивался ценой последнего прихода.
        #
        # Скрытые материалы не считаем: администратор нажал «Удалить», и товар
        # для него больше не существует. Оставлять его в стоимости склада —
        # это «удалил, а он в отчётах», ровно то, на что жаловался заказчик.
        live_materials = Material.objects.filter(is_archived=False)
        stock_value = sum(
            (m.stock_value for m in live_materials.prefetch_related("rolls")),
            Decimal("0"),
        )

        # Выручка по способам оплаты (нал / MBank / DemirBank / онлайн).
        def rev(method):
            return paid.filter(payment_method=method).aggregate(v=_ZERO)["v"]

        revenue_cash = rev(Receipt.PaymentMethod.CASH)
        revenue_mbank = rev(Receipt.PaymentMethod.MBANK)
        revenue_demirbank = rev(Receipt.PaymentMethod.DEMIRBANK)
        revenue_online = rev(Receipt.PaymentMethod.ONLINE)
        revenue_total = revenue_cash + revenue_mbank + revenue_demirbank + revenue_online

        # Разбивка выручки — работа против материала — по тем же заказам, что и
        # выручка выше: все неотменённые, кроме возвращённых строк.
        paid_lines = by_period(
            TransactionItem.objects.filter(is_returned=False).exclude(
                receipt__status=Receipt.Status.CANCELLED
            ),
            field="receipt__created_at",
        )
        work_revenue = paid_lines.filter(type=TransactionItem.Type.SERVICE).aggregate(
            v=_LINE_SUM
        )["v"]
        material_lines = paid_lines.filter(type=TransactionItem.Type.MATERIAL)
        material_revenue = material_lines.aggregate(v=_LINE_SUM)["v"]
        # Себестоимость проданного материала — по ТЕМ ЖЕ строкам, что и выручка
        # (тот же период, только оплаченные и невозвращённые). Цифра снята в
        # момент списания со склада: для рулонных — по FIFO-партиям, откуда
        # материал реально ушёл. Одна выручка без неё не отвечала на вопрос
        # «сколько на материале заработали»: 149 232 сом продали — а купили их
        # почём?
        material_cost = material_lines.aggregate(v=_COST_SUM)["v"]

        service_items = by_period(
            TransactionItem.objects.filter(
                type=TransactionItem.Type.SERVICE, is_returned=False
            ),
            field="receipt__created_at",
        )
        services_count = service_items.count()

        # Material consumed by services, via technological cards.
        materials_consumed = Decimal("0")
        for item in service_items.select_related("service").prefetch_related(
            "service__recipes"
        ):
            for recipe in item.service.recipes.all():
                materials_consumed += recipe.consumption_per_unit * item.quantity

        refunded_total = by_period(Receipt.objects.all()).aggregate(
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

        # Виды материалов на исходе (остаток ≤ критического) — список, не только
        # счёт. Скрытые не показываем: докупать то, что удалили из каталога, не
        # нужно.
        low_stock_items = [
            {
                "id": m.id,
                "name": m.name,
                "quantity": m.quantity,
                "unit": m.unit,
                "critical_balance": m.critical_balance,
                "sheets_remaining": m.sheets_remaining,
            }
            for m in live_materials
            if m.is_below_critical
        ]

        return Response(
            {
                "unrealised_asset": stock_value,
                "revenue": {
                    "cash": revenue_cash,
                    "mbank": revenue_mbank,
                    "demirbank": revenue_demirbank,
                    "online": revenue_online,
                    "total": revenue_total,
                },
                "breakdown": {
                    "work_revenue": work_revenue,
                    # Материал: за сколько продали, почём он нам достался и что
                    # осталось. Прибыль тут ВАЛОВАЯ — до аренды, зарплат и
                    # прочих расходов; итоговую прибыль по-прежнему считают
                    # Финансы блоком «Материалы» (решение заказчика).
                    "material_revenue": material_revenue,
                    "material_cost": material_cost,
                    "material_profit": material_revenue - material_cost,
                },
                "services_performed": services_count,
                "materials_consumed_by_services": materials_consumed,
                "refunds": {
                    "total_refunded": refunded_total,
                    "material_lost_quantity": abs(lost_qty),
                },
                "low_stock_count": len(low_stock_items),
                "low_stock_items": low_stock_items,
            }
        )


class ClientPurchasesView(APIView):
    """GET /api/audit/client-purchases/ — per-client material purchase analytics.

    Admin-only. Aggregates paid, non-returned MATERIAL lines per client:
    total material spend, total area/qty, order count. Sortable via ?ordering=.
    """

    permission_classes = [IsAdminOrAccountantRead]

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
