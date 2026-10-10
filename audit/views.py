from datetime import date
from decimal import Decimal

from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminOrAccountantRead
from sales import reporting
from sales.models import Receipt, TransactionItem

from .filters import AuditLogFilter
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
    """Журнал действий. Админ и бухгалтер (только чтение).

    Фильтры: ?date_from= ?date_to= (по дню записи), ?user=, ?kind= (тип:
    login, order, cash, expense, payroll, tax, settings, stock, price, client,
    staff, other), ?search= (по тексту). Страницы: ?page=, ?page_size=."""

    queryset = AuditLog.objects.select_related("user").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_class = AuditLogFilter
    ordering = ["-created_at", "-id"]

    @action(detail=False, methods=["get"])
    def export(self, request):
        """GET /audit/logs/export/ — CSV журнала с теми же фильтрами (даты,
        пользователь, тип, поиск), все страницы сразу (волна 2): «;», BOM,
        дата и время по местному времени, ячейки-формулы экранированы."""
        from django.utils import timezone

        from clients.export import safe
        from finance.exports import csv_response

        from .kinds import classify

        rows = [["Дата и время", "Пользователь", "Тип", "Действие"]]
        for log in self.filter_queryset(self.get_queryset()).order_by("-created_at", "-id"):
            rows.append([
                timezone.localtime(log.created_at),
                safe(log.user.username if log.user_id else ""),
                log.kind or classify(log.action),
                safe(log.action),
            ])
        return csv_response(rows, f"zhurnal-{timezone.localdate():%Y-%m-%d}.csv")


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


# Единицы количества в «Покупках по клиентам» (как в чеке) и порядок вывода.
UNIT_LABELS = {"PIECE": "шт", "SQM": "кв.м", "METER": "пог.м", "KG": "кг", "LITER": "л"}
UNIT_ORDER = {"PIECE": 0, "SQM": 1, "METER": 2, "KG": 3, "LITER": 4}


def _unit_code(sale_mode, is_roll, material_unit):
    """Единица материальной строки — по тем же правилам, что в чеке."""
    if sale_mode == TransactionItem.SaleMode.PIECE:
        return "PIECE"
    if sale_mode == TransactionItem.SaleMode.METER:
        return "METER"
    if is_roll:
        return "SQM"
    return material_unit or "PIECE"


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
            for client_id, qty, price, mode, roll, unit in lines.values_list(
                "receipt__client", "quantity", "price_per_item", "sale_mode",
                "material__is_roll_material", "material__unit",
            ):
                acc = by_client.setdefault(
                    client_id, {"spend": Decimal("0"), "qty": {}}
                )
                acc["spend"] += sign * TransactionItem(quantity=qty, price_per_item=price).sold_total
                # Количество — по ЕДИНИЦАМ: 20 листов и 0,96 кв.м куска не
                # складываются в «20,96» (CLI-10). Единица строки — та же, что
                # в чеке (`TransactionItemSerializer.get_unit_code`).
                code = _unit_code(mode, roll, unit)
                acc["qty"][code] = acc["qty"].get(code, Decimal("0")) + sign * qty

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
                # Старое поле осталось для сортировки: число есть, только когда
                # все строки клиента в одной единице; иначе — пусто, и смотреть
                # надо `qty_by_unit` / `material_qty_label`.
                "material_qty": next(iter(acc["qty"].values())) if len(acc["qty"]) == 1 else None,
                "qty_by_unit": [
                    {"unit": code, "label": UNIT_LABELS.get(code, code), "qty": value}
                    for code, value in sorted(acc["qty"].items(), key=lambda kv: UNIT_ORDER.get(kv[0], 9))
                    if value
                ],
                "material_qty_label": " + ".join(
                    f"{value.normalize():f} {UNIT_LABELS.get(code, code)}"
                    for code, value in sorted(acc["qty"].items(), key=lambda kv: UNIT_ORDER.get(kv[0], 9))
                    if value
                ),
                "orders": orders,
            })

        reverse = ordering.startswith("-")
        key = ordering.lstrip("-")
        result.sort(
            key=lambda x: x[key].lower() if key == "client_name" else (x[key] if x[key] is not None else Decimal("0")),
            reverse=reverse,
        )
        return Response(result)
