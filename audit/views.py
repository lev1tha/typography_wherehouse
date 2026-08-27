from decimal import Decimal

from django.db.models import DecimalField, F, Q, Sum
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
_REFUNDED = Coalesce(Sum("refunded_amount"), Decimal("0"), output_field=DecimalField())


def _line_sum(items) -> Decimal:
    """Выручка строк — вверх до целого сома, как `TransactionItem.line_total`
    и итог чека. Раньше складывались сырые qty × price (147.6 вместо 148), и
    на одном экране «материал 3 142 + работа 592» не давали «выручку 3 735».
    Считаем в Python по Decimal, а не CEIL в базе: SQLite умножает в double и
    0.554 × 1500 даёт 831.0000000000001 → 832 — тот же шум, от которого ушли
    в кассе."""
    return sum((it.line_total for it in items.only("quantity", "price_per_item", "is_returned")), Decimal("0"))

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
        # ДВА разных набора материалов, и путать их нельзя:
        #  · `stock_materials` — что лежит на складе и стоит денег. Скрытый
        #    материал с остатком СЮДА ВХОДИТ: он физически на полке. Раньше он
        #    выпадал целиком, и нажатие «Удалить» на позиции с товаром мгновенно
        #    уменьшало активы — 7 201 сом исчезали одним кликом, хотя материал
        #    никуда не делся. Исходная жалоба («удалил, а он в отчётах») закрыта
        #    другим: материал БЕЗ продаж удаляется насовсем вместе с приходами,
        #    а прячется только тот, по которому продажи были, — настоящий товар.
        #    Опустевший скрытый материал даёт ноль и так.
        #  · `live_materials` — чем цех торгует. Скрытого тут нет: докупать то,
        #    что убрали из каталога, не нужно.
        stock_materials = Material.objects.filter(
            Q(is_archived=False) | Q(quantity__gt=0)
        )
        live_materials = Material.objects.filter(is_archived=False)
        stock_value = sum(
            (m.stock_value for m in stock_materials.prefetch_related("rolls")),
            Decimal("0"),
        )

        # Выручка по способам оплаты (нал / MBank / DemirBank / онлайн) — за
        # вычетом возвращённых строк, как «Выручка» в Финансах: до этого Обзор
        # показывал 6 231 там, где Финансы — 5 331 (частичный возврат на 900).
        def rev(method):
            qs = paid.filter(payment_method=method)
            return qs.aggregate(v=_ZERO)["v"] - qs.aggregate(v=_REFUNDED)["v"]

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
        work_revenue = _line_sum(paid_lines.filter(type=TransactionItem.Type.SERVICE))
        material_lines = paid_lines.filter(type=TransactionItem.Type.MATERIAL)
        material_revenue = _line_sum(material_lines)
        # Себестоимость проданного материала — по ТЕМ ЖЕ строкам, что и выручка
        # (тот же период, только оплаченные и невозвращённые). Цифра снята в
        # момент списания со склада: для рулонных — по FIFO-партиям, откуда
        # материал реально ушёл. Одна выручка без неё не отвечала на вопрос
        # «сколько на материале заработали»: 149 232 сом продали — а купили их
        # почём?
        material_cost = material_lines.aggregate(v=_COST_SUM)["v"]
        # Себестоимость ВСЕГО проданного — той же формулой, что в «Финансах»
        # (`cogs`): вместе со строками работы, у которых своя себестоимость по
        # техкарте. Блок «Сколько заработали на материале» ниже по-прежнему
        # считает только материал — он и отвечает на вопрос про материал.
        cogs_total = paid_lines.aggregate(v=_COST_SUM)["v"]

        service_items = by_period(
            TransactionItem.objects.filter(
                type=TransactionItem.Type.SERVICE, is_returned=False
            ),
            field="receipt__created_at",
        )
        services_count = service_items.count()

        # Material consumed by services, via technological cards — той же
        # формулой, что списывает склад: «на кв.м» от площади куска, «фикс» раз
        # на строку (у резки `quantity` — погонные метры, не площадь).
        from sales.sale_service import recipe_consumption

        materials_consumed = Decimal("0")
        for item in service_items.select_related("service").prefetch_related(
            "service__recipes"
        ):
            for recipe in item.service.recipes.all():
                materials_consumed += recipe_consumption(recipe, item)

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
        # «На исходе» и «нет в наличии» считаем врозь — как каталог: ноль там
        # спокойный факт (только что заведённый каталог весь на нуле), красное
        # — когда остаток есть, но упал до порога. Плитка «Материалов на
        # исходе: 16» на свежей базе с одним материалом на исходе выглядела
        # аварией и спорила со складом, где на исходе был один.
        out_of_stock_count = sum(1 for m in low_stock_items if m["quantity"] <= 0)
        low_stock_count = len(low_stock_items) - out_of_stock_count

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
                    # ПРИБЫЛЬ ДО РАСХОДОВ — выручка минус себестоимость всего
                    # проданного (решение владельца, 2026-08-27). Одной выручки
                    # наверху было мало: «5 525» читалось как заработок, хотя
                    # 2 955 из них — деньги, за которые материал купили. Это
                    # НЕ выручка (выручка честно равна 5 525) и НЕ итоговая
                    # прибыль (из неё ещё вычитаются аренда и зарплаты).
                    #
                    # Себестоимость берём по ВСЕМ строкам, а не только по
                    # материалу: услуга списывает по техкарте свой расходник
                    # (клей, крепёж), и его стоимость сидит в строке РАБОТЫ.
                    # Считать её только по материалу значило бы разойтись с
                    # «Финансами» — а это одно и то же число на двух экранах.
                    "profit_before_expenses": revenue_total - cogs_total,
                    # Себестоимость всего проданного — чтобы подпись под плиткой
                    # объясняла её же формулой, а не складывала работу с
                    # материалом: расходники услуги в такую сумму не попадают,
                    # и подпись расходилась с плиткой на их стоимость.
                    "cogs_total": cogs_total,
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
                "low_stock_count": low_stock_count,
                "out_of_stock_count": out_of_stock_count,
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
        # `_line_sum`); собираем по клиентам в Python — набор небольшой.
        by_client = {}
        lines = TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL,
            is_returned=False,
            receipt__in=live,
            receipt__client__isnull=False,
        ).values_list("receipt__client", "quantity", "price_per_item")
        for client_id, qty, price in lines:
            acc = by_client.setdefault(client_id, {"spend": Decimal("0"), "qty": Decimal("0")})
            acc["spend"] += TransactionItem(quantity=qty, price_per_item=price).line_total
            acc["qty"] += qty

        # Attach client display data + order count, then sort in Python (small set).
        from clients.models import Client

        clients = {c.id: c for c in Client.objects.filter(id__in=by_client.keys())}
        result = []
        for client_id, acc in by_client.items():
            client = clients.get(client_id)
            if not client:
                continue
            orders = live.filter(client=client).count()
            result.append({
                "client_id": client.id,
                "client_name": client.display_name,
                "phone": client.phone,
                "material_spend": acc["spend"],
                "material_qty": acc["qty"],
                "orders": orders,
            })

        reverse = ordering.startswith("-")
        key = ordering.lstrip("-")
        result.sort(key=lambda x: x[key] if key != "client_name" else x[key].lower(), reverse=reverse)
        return Response(result)
