from datetime import date
from decimal import Decimal

from django.db.models import Count
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminOrAccountantRead
from sales import reporting
from sales.models import Receipt, TransactionItem

from .models import AuditLog
from .serializers import AuditLogSerializer


def _parse_day(value):
    """«2026-08-31» → date. Мусор и пустота — как будто периода нет."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Admin-only hidden trail of staff actions."""

    queryset = AuditLog.objects.select_related("user").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_fields = ["user"]
    search_fields = ["action"]
    ordering = ["-created_at"]


class DashboardView(APIView):
    """GET /api/audit/dashboard/?date_from=&date_to= — «Обзор».

    Главные плитки с динамикой к прошлому периоду (`headline`) и прежние
    разборы (`dashboard`) — оба из `finance.reports.overview`: те же функции
    ОПиУ, ОДДС и сверки, что и в «Финансах»."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        from finance.reports.overview import dashboard, headline

        d_from = _parse_day(request.query_params.get("date_from"))
        d_to = _parse_day(request.query_params.get("date_to"))
        return Response({**dashboard(d_from, d_to), "headline": headline(d_from, d_to)})


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

        # Та же база, что у «Продали материала на …» в шапке Обзора: все
        # заказы периода, кроме отменённых, без возвращённых строк. Раньше сюда
        # шли только ОПЛАЧЕННЫЕ чеки, и сумма таблицы (2 312) не сходилась с
        # цифрой выше (3 142) — заказ в долг материал уже забрал, а в «покупках»
        # его не было. Период — тот же, что у остальных денежных плиток.
        date_from = request.query_params.get("date_from") or None
        date_to = request.query_params.get("date_to") or None
        live = Receipt.objects.exclude(status=Receipt.Status.CANCELLED)
        if date_from:
            live = live.filter(created_at__date__gte=date_from)
        if date_to:
            live = live.filter(created_at__date__lte=date_to)

        # Суммы строк — вверх до сома по Decimal, как в шапке Обзора (см.
        # `finance.reports.overview._line_sum` — одна реализация на оба экрана); собираем по клиентам в Python — набор небольшой.
        #
        # Заказы БЕЗ КЛИЕНТА (продажа с улицы) идут одной общей строкой, а не
        # выбрасываются: без неё сумма таблицы не сходилась с плиткой «Продали
        # материала на …» над ней — на проде 19.09 это 467 263 против 474 274.
        # Разницу в 7 011 объяснить было нечем, и обе цифры выглядели
        # неправильными, хотя каждая считалась верно.
        #
        # Возвраты — по дате возврата, как во всех денежных цифрах
        # (`sales.reporting`): строка, возвращённая позже периода, в нём ещё
        # продажа; возвращённая в периоде — минус у того клиента, чей заказ.
        by_client = {}
        d_from, d_to = _parse_day(date_from), _parse_day(date_to)
        material = TransactionItem.Type.MATERIAL
        signed = [
            (1, TransactionItem.objects.filter(type=material, is_returned=False, receipt__in=live)),
            (1, reporting.added_back(d_from, d_to).filter(type=material)),
            (-1, reporting.returned_lines(d_from, d_to).filter(type=material)),
        ]
        for sign, lines in signed:
            for client_id, qty, price in lines.values_list(
                "receipt__client", "quantity", "price_per_item"
            ):
                acc = by_client.setdefault(client_id, {"spend": Decimal("0"), "qty": Decimal("0")})
                acc["spend"] += sign * TransactionItem(quantity=qty, price_per_item=price).sold_total
                acc["qty"] += sign * qty

        # Attach client display data + order count, then sort in Python (small set).
        from clients.models import Client

        clients = {c.id: c for c in Client.objects.filter(id__in=by_client.keys())}
        # Число заказов по всем клиентам — одним GROUP BY, а не запросом на
        # клиента (на 800 клиентах это было 803 запроса).
        orders_by_client = dict(
            live.values_list("client").annotate(n=Count("id")).order_by().values_list("client", "n")
        )
        result = []
        for client_id, acc in by_client.items():
            client = clients.get(client_id)
            if client_id and not client:
                continue
            orders = orders_by_client.get(client.id if client else None, 0)
            result.append({
                # У строки «без клиента» `client_id` пустой — по нему интерфейс
                # и отличает её от обычной: ни карточки, ни телефона у неё нет.
                "client_id": client.id if client else None,
                "client_name": client.display_name if client else "Без клиента",
                "phone": client.phone if client else "",
                "material_spend": acc["spend"],
                "material_qty": acc["qty"],
                "orders": orders,
            })

        reverse = ordering.startswith("-")
        key = ordering.lstrip("-")
        result.sort(key=lambda x: x[key] if key != "client_name" else x[key].lower(), reverse=reverse)
        return Response(result)
