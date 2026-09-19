from collections import defaultdict
from decimal import Decimal

from django.db.models import DecimalField, F, Sum
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminOrAccountantRead
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, Material, stock_value_total

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
        # то есть весь остаток оценивался ценой последнего прихода. Считает её
        # `stock_value_total` — та же функция, что у «Склада (оборот)» в
        # «Финансах»: своя формула там разошлась с этой (15.09).
        #
        # ДВА разных набора материалов, и путать их нельзя:
        #  · стоимость склада — что лежит на складе и стоит денег. Скрытый
        #    материал с остатком СЮДА ВХОДИТ: он физически на полке. Раньше он
        #    выпадал целиком, и нажатие «Удалить» на позиции с товаром мгновенно
        #    уменьшало активы — 7 201 сом исчезали одним кликом, хотя материал
        #    никуда не делся. Исходная жалоба («удалил, а он в отчётах») закрыта
        #    другим: материал БЕЗ продаж удаляется насовсем вместе с приходами,
        #    а прячется только тот, по которому продажи были, — настоящий товар.
        #    Опустевший скрытый материал даёт ноль и так.
        #  · `live_materials` — чем цех торгует. Скрытого тут нет: докупать то,
        #    что убрали из каталога, не нужно.
        live_materials = Material.objects.filter(is_archived=False)
        stock_value = stock_value_total()

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

        # СКОЛЬКО ИЗ ЭТОГО УЖЕ ПОЛУЧИЛИ, а сколько ещё должны — тем же
        # разрезом. Без этих двух строк «Наличные 449 042» читаются как деньги
        # в ящике, а это СТОИМОСТЬ ЗАКАЗОВ: на проде 19.09 из них не заплачено
        # 314 141, и в ящике лежало 114 707. Плитка и касса спорили вчетверо,
        # и это первое, что заказчик складывает в уме.
        #
        # Способ у полученных денег берём НЕ у чека, а у самой оплаты: долг
        # часто гасят не тем способом, которым оформляли заказ (наличный заказ
        # закрывают переводом). Первая оплата при оформлении своей записи
        # `Payment` не заводит — это остаток суммы чека сверх записанных
        # погашений, и он идёт способом чека. Ровно так же разносит деньги
        # кассовая книга (`finance.cash.account_for`), поэтому «получено
        # наличными» здесь и наличный остаток кассы — об одном и том же.
        received = defaultdict(lambda: Decimal("0"))
        debt = defaultdict(lambda: Decimal("0"))
        for r in paid.prefetch_related("payments"):
            kept = r.total_price - r.refunded_amount
            # Больше, чем стоит оставшийся заказ, выручкой не считаем: лишнее —
            # это сдача или возвращённые деньги, у них свои поля (та же
            # оговорка, что у `revenue_paid` в «Финансах»).
            cap = min(r.amount_paid, kept) if kept > 0 else Decimal("0")
            rows = []
            payments = sorted(r.payments.all(), key=lambda p: (p.paid_on, p.id))
            first = r.amount_paid - sum((p.amount for p in payments), Decimal("0"))
            if first > 0:
                rows.append((r.payment_method, first))
            rows += [(p.method, p.amount) for p in payments]
            left = cap
            for method, amount in rows:
                if left <= 0:
                    break
                take = min(amount, left)
                received[method] += take
                left -= take
            owed = kept - r.amount_paid
            if r.payment_status in Receipt.OWING_STATUSES and owed > 0:
                debt[r.payment_method] += owed

        def split(source):
            """Четыре способа и итог — одной формой, как у выручки выше."""
            out = {
                "cash": source[Receipt.PaymentMethod.CASH],
                "mbank": source[Receipt.PaymentMethod.MBANK],
                "demirbank": source[Receipt.PaymentMethod.DEMIRBANK],
                "online": source[Receipt.PaymentMethod.ONLINE],
            }
            out["total"] = sum(out.values(), Decimal("0"))
            return out

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

        # СПИСАНО МИМО ПРОДАЖИ — недостача по инвентаризации и брак.
        #
        # Главное здесь ДЕНЬГИ: материал, вынесенный со склада правкой остатка,
        # не попадает ни в себестоимость, ни в расходы, и склад просто худеет.
        # На проде 19.09 так ушло 103 424 сома — и не было экрана, где эта
        # сумма появлялась хоть раз. Себестоимость движения знает журнал: у
        # площадных — по партиям, у штучных — по закупочной; у записей до
        # 04.09 её нет, поэтому «сколько» и «на сколько» — разные ответы.
        #
        # Количество складывать по материалам НЕЛЬЗЯ (штуки диодов с
        # квадратными метрами акрила), поэтому наружу отдаём только деньги и
        # число записей. Период — тот же, что у остальных денежных плиток:
        # раньше эта цифра считалась по всей истории и с ними не дружила.
        losses = by_period(
            InventoryLog.objects.filter(
                type__in=[InventoryLog.Type.ADJUSTMENT, InventoryLog.Type.WRITE_OFF],
                quantity_changed__lt=0,
            ),
            field="happened_at",
        )
        lost_cost = losses.aggregate(
            v=Coalesce(Sum("cost"), Decimal("0"), output_field=DecimalField())
        )["v"]
        lost_rows = losses.count()
        lost_unknown = losses.filter(cost__isnull=True).count()

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
                    # Выручка — это ЗАКАЗЫ периода, включая отданные в долг.
                    # Сколько из них уже на руках и сколько ещё должны — тем же
                    # разрезом, чтобы плитку нельзя было прочитать как кассу.
                    "received": split(received),
                    "debt": split(debt),
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
                    # Сколько денег вынесли со склада мимо продажи, сколько
                    # таких записей и у скольких из них себестоимость
                    # неизвестна (списания до 04.09).
                    "material_lost_cost": lost_cost,
                    "material_lost_rows": lost_rows,
                    "material_lost_unknown": lost_unknown,
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
        #
        # Заказы БЕЗ КЛИЕНТА (продажа с улицы) идут одной общей строкой, а не
        # выбрасываются: без неё сумма таблицы не сходилась с плиткой «Продали
        # материала на …» над ней — на проде 19.09 это 467 263 против 474 274.
        # Разницу в 7 011 объяснить было нечем, и обе цифры выглядели
        # неправильными, хотя каждая считалась верно.
        by_client = {}
        lines = TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL,
            is_returned=False,
            receipt__in=live,
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
            if client_id and not client:
                continue
            orders = (
                live.filter(client=client).count()
                if client
                else live.filter(client__isnull=True).count()
            )
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
