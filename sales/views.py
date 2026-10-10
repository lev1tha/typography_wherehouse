from decimal import Decimal

from django.db import transaction
from django.db.models import (
    Case, DecimalField, Exists, F, IntegerField, OuterRef, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdmin, IsNotAccountant
from audit.models import AuditLog
from finance import cash
from finance.periods import ensure_open
from clients.models import Client
from clients.phones import find_client_by_phone
from clients.serializers import ClientSerializer
from integrations.telegram import notify_customer, send_customer_receipt
from warehouse.models import InventoryLog
from warehouse.rolls import InsufficientStock

from . import idempotency
from .models import Receipt, TransactionItem
from .reporting import day_after, day_start
from .sale_service import (
    DeleteRejected,
    ItemEditRejected,
    OrderClosed,
    PaymentRejected,
    add_items_to_receipt,
    apply_payment,
    create_sale,
    day_to_moment,
    delete_receipt,
    give_change,
    lock_receipt,
    parse_amount,
    parse_paid_on,
    receipt_summary,
    refund_receipt,
    return_applied_change,
    strip_cost,
    update_receipt_items,
)
from .serializers import (
    RefundSerializer,
    ReceiptSerializer,
    SaleCreateSerializer,
    SaleItemInputSerializer,
)

# Порядок производства. Нужен ровно для одного: понять, поехал заказ вперёд или
# его вернули назад, — от этого зависит, что написать клиенту.
_FULFILLMENT_ORDER = {
    Receipt.FulfillmentStatus.PROCESSING: 0,
    Receipt.FulfillmentStatus.READY: 1,
    Receipt.FulfillmentStatus.ISSUED: 2,
}


def _is_rollback(was: str, now: str) -> bool:
    return _FULFILLMENT_ORDER[now] < _FULFILLMENT_ORDER[was]


_READY = "✅ Ваш заказ по резке букв успешно выполнен и ждёт вас на складе!"
_ISSUED = "📦 Ваш заказ выдан. Спасибо, что выбрали нас!"

# Что уходит клиенту на каждый переход. Ключ — пара «откуда, куда», а не просто
# «куда»: клиенту, которому уже написали «заказ готов», второе такое же
# сообщение ничего не объясняет. Назад заказ едет только из-за ошибки цеха, и
# сказать об этом должен сам текст — иначе клиент выйдет из дома зря.
FULFILLMENT_MESSAGES = {
    (Receipt.FulfillmentStatus.PROCESSING, Receipt.FulfillmentStatus.READY): _READY,
    (Receipt.FulfillmentStatus.PROCESSING, Receipt.FulfillmentStatus.ISSUED): _ISSUED,
    (Receipt.FulfillmentStatus.READY, Receipt.FulfillmentStatus.ISSUED): _ISSUED,
    (Receipt.FulfillmentStatus.READY, Receipt.FulfillmentStatus.PROCESSING): (
        "🔧 Мы поторопились: заказ ещё в работе. Сообщим, когда он будет готов."
    ),
    (Receipt.FulfillmentStatus.ISSUED, Receipt.FulfillmentStatus.PROCESSING): (
        "🔧 Отметка о выдаче была ошибкой — заказ ещё в работе. "
        "Сообщим, когда он будет готов."
    ),
    (Receipt.FulfillmentStatus.ISSUED, Receipt.FulfillmentStatus.READY): (
        "✅ Отметка о выдаче была ошибкой — заказ ждёт вас на складе."
    ),
}


def _receipt_lines_text(receipt: Receipt) -> str:
    lines = []
    for item in receipt.items.all():
        target = item.material.name if item.material_id else item.service.name
        lines.append(f"• {target} × {item.quantity} = {item.line_total} сом")
    return "\n".join(lines)


def _parse_date(value):
    """'YYYY-MM-DD' → date, иначе None (пустой/битый ввод = без фильтра)."""
    from datetime import date as _date

    try:
        return _date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


# На сколько назад разрешено датировать заказ. Задним числом заказ проводят,
# когда его не успели занести в тот же день, — это дни и недели, а не годы.
# Верхняя граница (будущее) была, нижней не было вовсе, и опечатка в году
# спокойно уводила заказ в 2015-й, переписывая выручку и складской лист месяца,
# в который никто уже не заглядывает.
MAX_BACKDATE_DAYS = 366


def _price_override_forbidden(items, user):
    """Текст ошибки, если не-админ прислал ручную цену или ставку, иначе None.

    Цену материала и ставку резки в момент продажи правит только админ — в
    кассе у складовщика этих полей нет. Но API их принимал от кого угодно:
    складовщик с консолью браузера оформлял лист и резку за 0 при
    себестоимости 4 087. Молча выбрасывать значения нельзя — отвечаем отказом,
    чтобы расхождение «что просили / что оформили» было видно сразу.
    """
    if user.is_admin_role:
        return None
    only_admin = "Цену материала и ставку резки при продаже правит только администратор."
    for item in items:
        if item.get("material_price") is not None:
            return only_admin
        rate = item.get("cut_rate")
        if rate is None:
            continue
        if not _rate_open_to_staff(item):
            return only_admin
        # Цену складовщик называет, а ПОДАРОК остаётся правом админа. Ноль
        # здесь — это работа бесплатно: интерфейс её не даёт добавить (кнопка
        # закрыта, пока цена не больше нуля), но API принимал её от кого
        # угодно, и консоли браузера хватало, чтобы отдать резку даром. Ровно
        # эту дверь закрыл аудит 18.08 (п. 14) для обычных строк; открыв цену
        # складовщику 04.09, мы её приоткрыли снова.
        if Decimal(str(rate)) <= 0:
            return (
                "Работа за 0 сом — это подарок, и оформить его может только "
                "администратор. Укажите цену больше нуля."
            )
    return None


def _order_pricing(data, client, user):
    """Правила прайса нового заказа: (срочно, наценка %, скидка %) или отказ.

    Отказ — пара (текст, HTTP-код). Скидка не прислана — подставляется
    скидка из карточки клиента. Свою скидку (другую, чем в карточке, в том
    числе снять её) задаёт только админ: складовщик применяет настроенную —
    как и ручную цену правит только админ (`_price_override_forbidden`).
    Срочность ставит любой, но только когда владелец задал наценку: при 0
    переключатель в кассе закрыт, и молча оформить «срочно за 0 %» значило бы
    обмануть того, кто его нажал.
    """
    from services.models import PricingSettings

    urgent = bool(data.get("is_urgent"))
    urgency = Decimal("0")
    if urgent:
        urgency = PricingSettings.load().urgency_percent
        if urgency <= 0:
            return None, (
                "Наценка за срочность не задана — задайте процент в «Ценах и "
                "услугах» или оформите заказ без «Срочно».",
                status.HTTP_400_BAD_REQUEST,
            )
    configured = client.discount_percent if client is not None else Decimal("0")
    asked = data.get("discount_percent")
    discount = configured if asked is None else asked
    if discount != configured and not user.is_admin_role:
        return None, (
            "Скидку на заказ задаёт только администратор. Складовщик применяет "
            "скидку из карточки клиента.",
            status.HTTP_403_FORBIDDEN,
        )
    return (urgent, urgency, discount), None


def _rate_open_to_staff(item) -> bool:
    """Ставку этой строки складовщик вписывает сам (2026-09-04, решение
    владельца): резка МАТЕРИАЛА КЛИЕНТА — каталожной ставки у чужого листа
    нет, цену называют на месте; ГРАВИРОВКА — у крупных заказов цена за кв.м
    своя. Всё остальное — по-прежнему только админ (аудит 18.08, п. 14)."""
    if item.get("own_material"):
        return True
    service = item.get("service")
    return bool(service is not None and getattr(service, "staff_sets_rate", False))


def _check_backdate(day):
    """Вернуть текст ошибки, если дата заказа слишком старая, иначе None."""
    from datetime import timedelta

    if day is None:
        return None
    floor = timezone.localdate() - timedelta(days=MAX_BACKDATE_DAYS)
    if day < floor:
        return (
            f"Дата заказа слишком старая: раньше {floor.strftime('%d.%m.%Y')} "
            "заказы не проводятся. Проверьте год."
        )
    return None


def _normalize_query(raw: str) -> str:
    """Запрос поиска без «№», «#» и пробелов по краям: «№100» = «100»."""
    return (raw or "").replace("№", " ").replace("#", " ").strip()


def _search_number(raw: str):
    """Число, если запрос — только цифры (после «№»/«#»/пробелов), иначе None."""
    query = _normalize_query(raw).replace(" ", "")
    return query if query.isdigit() else None


# Столько цифр и меньше — это номер чека, а не кусок телефона: телефон короче
# четырёх цифр никто не набирает, зато «5» раньше находило 242 заказа из 280
# (любой номер, телефон или название с пятёркой внутри).
SHORT_NUMBER_DIGITS = 3


def _same_name(a: str, b: str) -> bool:
    fold = lambda x: " ".join((x or "").casefold().split())  # noqa: E731
    return fold(a) == fold(b)


def _name_differs(client, client_data) -> bool:
    """Введённое в кассе имя не совпадает с именем найденного по телефону клиента.

    Сравниваем без учёта регистра и лишних пробелов, и с любым из имён карточки
    (ФИО, компания, отображаемое). Имя не вводили — расхождения нет.
    """
    typed = (client_data.get("full_name") or client_data.get("company_name") or "").strip()
    if not typed:
        return False
    known = (client.full_name, client.company_name, client.display_name)
    return not any(name and _same_name(typed, name) for name in known)


class ReceiptSearchFilter(SearchFilter):
    """Поиск чеков: цифры — это номер заказа, а не подстрока чего угодно.

    - «№100», «#100», « 100 » — то же, что «100»;
    - до трёх цифр — ТОЧНЫЙ номер чека;
    - от четырёх цифр — точный номер ИЛИ вхождение в телефон клиента;
    - всё остальное (слова) — как раньше: название, клиент, компания, телефон.
    """

    def filter_queryset(self, request, queryset, view):
        raw = request.query_params.get(self.search_param, "")
        digits = _search_number(raw)
        if digits is None:
            return super().filter_queryset(request, queryset, view)
        number = int(digits)
        exact = Q(order_number=number) if number < 2**31 else Q(pk__in=[])
        if len(digits) <= SHORT_NUMBER_DIGITS:
            return queryset.filter(exact)
        return queryset.filter(exact | Q(client__phone__icontains=digits))


class ReceiptViewSet(viewsets.ModelViewSet):
    """Sales / receipts. Storekeepers create sales and issue refunds; admins
    see everything with filtering by date, cashier, payment method and status.

    Фильтры списка: `?client=` (карточка клиента), `?date_from=&date_to=` (дата
    ЗАКАЗА, не оплаты), плюс способ оплаты и статус. Клиента и период добавили
    потому, что «найти все заказы Тахира за июль» через поиск по строке не
    делается: поиск ищет одно слово, а не пересечение двух условий.
    """

    queryset = Receipt.objects.select_related("client", "cashier").prefetch_related(
        "items__material", "items__service", "items__roll", "payments"
    )
    serializer_class = ReceiptSerializer
    # Бухгалтер чеки видит (с себестоимостью и маржой), но не оформляет: он
    # проверяющий, а не участник продажи.
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filterset_fields = ["payment_method", "payment_status", "status", "cashier", "client"]
    filter_backends = [DjangoFilterBackend, ReceiptSearchFilter, OrderingFilter]
    search_fields = ["order_number", "title", "client__phone", "client__full_name", "client__company_name"]
    # По умолчанию: у кого долг выше — тот вверху, затем по дате (новые выше).
    # Долг — вычисляемое поле, поэтому аннотируем `_debt` в get_queryset.
    ordering = ["-_debt", "-created_at"]
    # Разрешённые колонки для сортировки по клику (?ordering=...).
    ordering_fields = ["_debt", "created_at", "total_price", "change_due"]

    def get_queryset(self):
        qs = super().get_queryset()
        # Период — по ДАТЕ ЗАКАЗА (`created_at`), той же, по которой считаются
        # выручка и складской лист. Заказ задним числом ищется по своей дате, а
        # не по дню, когда его завели.
        d_from = _parse_date(self.request.query_params.get("date_from"))
        d_to = _parse_date(self.request.query_params.get("date_to"))
        # Диапазон по границам местных суток, а не `created_at__date`: так
        # работает индекс, а набор чеков тот же.
        if d_from:
            qs = qs.filter(created_at__gte=day_start(d_from))
        if d_to:
            qs = qs.filter(created_at__lt=day_after(d_to))
        # ?has_change=1 — только заказы, по которым цех не вернул сдачу. Это
        # рабочий список кассира: «кому мы ещё должны отдать».
        if self.request.query_params.get("has_change") == "1":
            qs = qs.filter(change_due__gt=0)
        # Чеки видит весь персонал, а не только тот, кто их оформил.
        #
        # Раньше складовщику отдавались только его собственные чеки, и в цехе на
        # двух человек это ломало выдачу: заказ принял админ, выдаёт складовщик —
        # а найти заказ он не может, у него пустой экран. Кто оформил, видно в
        # самом чеке отдельной колонкой.
        #
        # Деньги при этом остаются за админом: оплата долга и откат оплаты
        # закрыты отдельной проверкой, финансовые разделы — паролем.
        # Долг = остаток (сумма − оплачено − возвраты) для открытых чеков, иначе 0.
        # Совпадает с логикой свойства Receipt.debt; используется для сортировки.
        return qs.annotate(
            # Признак «есть услуга» — одним запросом на страницу, а не
            # по запросу на каждый чек (`Receipt.has_service`).
            _has_service=Exists(
                TransactionItem.objects.filter(
                    receipt=OuterRef("pk"), type=TransactionItem.Type.SERVICE
                )
            ),
            _debt=Case(
                When(
                    Q(payment_status__in=Receipt.OWING_STATUSES)
                    & ~Q(status=Receipt.Status.CANCELLED)
                    & Q(revenue_recognized_at__isnull=False)
                    & Q(total_price__gt=F("amount_paid") + F("refunded_amount")),
                    then=F("total_price") - F("amount_paid") - F("refunded_amount"),
                ),
                default=Value(Decimal("0")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            )
        )

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        # Поиск по номеру: чек с точным совпадением — первым, если порядок не
        # задан явно (?ordering=...). Иначе при «1234» точный №1234 тонет среди
        # заказов с этим числом в телефоне.
        digits = _search_number(self.request.query_params.get("search", ""))
        if digits and int(digits) < 2**31 and "ordering" not in self.request.query_params:
            qs = qs.annotate(
                _exact=Case(
                    When(order_number=int(digits), then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            ).order_by("_exact", *qs.query.order_by)
        return qs

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Сводка над списком чеков: всего / в работе / готово / долг. Считается
        по ВСЕМ чекам (с учётом роли и фильтров поиска), а не по одной странице."""
        qs = self.filter_queryset(self.get_queryset())
        active = qs.exclude(status=Receipt.Status.CANCELLED)
        working = (
            active.filter(
                fulfillment_status=Receipt.FulfillmentStatus.PROCESSING,
                items__type=TransactionItem.Type.SERVICE,
            )
            .distinct()
            .count()
        )
        ready = (
            active.filter(
                fulfillment_status=Receipt.FulfillmentStatus.READY,
                items__type=TransactionItem.Type.SERVICE,
            )
            .distinct()
            .count()
        )
        debt = Decimal("0")
        pending = active.filter(
            payment_status__in=Receipt.OWING_STATUSES, revenue_recognized_at__isnull=False
        ).values_list(
            "total_price", "amount_paid", "refunded_amount"
        )
        for total, paid, refunded in pending:
            owed = total - paid - refunded
            if owed > 0:
                debt += owed
        # Сдача — долг цеха ПЕРЕД клиентом, зеркальный обычному долгу, поэтому
        # стоит рядом с ним отдельной плиткой, а не прячется внутри чеков.
        # Возвращённые целиком заказы тоже считаем: деньги по ним лежат в
        # кассе, а зачёт сдачи в новый заказ их видит (`client_change_available`).
        change = qs.aggregate(
            v=Coalesce(Sum("change_due"), Decimal("0"), output_field=DecimalField())
        )["v"]
        return Response(
            {
                "total": qs.count(),
                "working": working,
                "ready": ready,
                "debt": debt,
                "change_due": change,
            }
        )

    @action(detail=False, methods=["get"])
    def titles(self, request):
        """Ранее использованные наименования заказов — для подсказки в кассе,
        как живой поиск по клиенту. Свежие сверху, без повторов."""
        seen, out = set(), []
        rows = (
            self.get_queryset()
            .exclude(title="")
            .order_by("-created_at")
            .values_list("title", flat=True)[:200]
        )
        for title in rows:
            key = title.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(title.strip())
            if len(out) >= 30:
                break
        return Response(out)

    def _fresh_response(self, receipt):
        """Re-load the receipt so the response reflects mutations (the loaded
        instance carries a stale prefetch cache after add/refund/status changes)."""
        fresh = self.get_queryset().get(pk=receipt.pk)
        return Response(ReceiptSerializer(fresh, context={"request": self.request}).data)

    # --- Правка и удаление ошибочного чека -------------------------------
    #
    # Раньше `PATCH`/`DELETE` были открыты всем авторизованным и без единой
    # проверки: складовщик мог переписать `total_price` любым числом, а DELETE
    # стирал чек, НЕ возвращая материал на склад. Кнопок в интерфейсе не было,
    # поэтому дыру никто не замечал — «изменить чек нельзя» и «чек защищён» это
    # разные вещи, и второго не было.
    #
    # Теперь: править можно только то, что не двигает деньги и склад, удалять —
    # целиком и с возвратом материала. И то и другое — админ.
    EDITABLE_FIELDS = {"title", "client", "order_date"}

    def create(self, request, *args, **kwargs):
        """Голый POST /receipts/ закрыт: чек продаётся только через /checkout/.

        ModelViewSet давал create бесплатно, и API принимал чек БЕЗ ПОЗИЦИЙ, но
        с «принятой» суммой: itemы у сериализатора read-only, а amount_paid —
        нет. Итог 0, оплачено 4000 — заказ-призрак с номером, который портит
        выручку «оплачено» и нумерацию. Интерфейс так не ходит, но токен
        админа позволял. Checkout же считает итог сам, списывает склад и пишет
        кассу — мимо него чекам появляться неоткуда.
        """
        return Response(
            {"detail": "Заказ оформляется только кассой: POST /api/sales/receipts/checkout/."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def update(self, request, *args, **kwargs):
        return self._edit_meta(request, partial=kwargs.get("partial", False))

    def partial_update(self, request, *args, **kwargs):
        return self._edit_meta(request, partial=True)

    def _edit_meta(self, request, *, partial):
        """PATCH /receipts/<id>/ — правка наименования, клиента и ДАТЫ ЗАКАЗА.

        Состав чека тут не меняется: пересчёт позиций тянет за собой списание со
        склада, себестоимость по партиям FIFO и уже принятые оплаты. Ошибочный
        состав исправляется удалением и повторным вводом — так ни одна из этих
        цифр не разъедется.
        """
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Править чеки может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # И сам чек, и дата, куда его двигают, должны быть в открытом периоде:
        # иначе правкой можно вынести деньги из закрытого месяца или занести их
        # туда.
        receipt = self.get_object()
        ensure_open(timezone.localtime(receipt.created_at), "Править заказ закрытого периода")
        if "order_date" in request.data:
            ensure_open(_parse_date(request.data.get("order_date")), "Перенести заказ этой датой")
        unknown = set(request.data) - self.EDITABLE_FIELDS
        if unknown:
            return Response(
                {
                    "detail": (
                        "Здесь можно поменять только наименование, клиента и дату "
                        "заказа. Ошибочный состав — удалить чек и завести заново. "
                        f"Отклонено: {', '.join(sorted(unknown))}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        receipt = self.get_object()
        # Меняем и пишем в журнал только то, что РЕАЛЬНО изменилось. Форма
        # правки шлёт все три поля разом, и раньше любое сохранение (даже
        # правка одного количества в составе) переставляло время заказа на
        # полдень — чек и его списание уезжали в хронологии, а журнал действий
        # уверял «client, order_date, title», хотя ничего из этого не трогали.
        changed = []
        if "title" in request.data:
            title = (request.data.get("title") or "").strip()[:255]
            if title != receipt.title:
                receipt.title = title
                changed.append("title")
        if "client" in request.data:
            raw = request.data.get("client")
            if raw in (None, "", 0):
                new_client_id = None
            elif Client.objects.filter(pk=raw).exists():
                new_client_id = int(raw)
            else:
                return Response(
                    {"detail": "Клиент не найден."}, status=status.HTTP_400_BAD_REQUEST
                )
            if new_client_id != receipt.client_id:
                receipt.client_id = new_client_id
                changed.append("client")
        moment = None
        if "order_date" in request.data:
            day = _parse_date(request.data.get("order_date"))
            if day is None:
                return Response(
                    {"detail": "Некорректная дата заказа."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if day > timezone.localdate():
                return Response(
                    {"detail": "Дата заказа не может быть в будущем."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            too_old = _check_backdate(day)
            if too_old:
                return Response({"detail": too_old}, status=status.HTTP_400_BAD_REQUEST)
            # Тот же день — время заказа не трогаем: полдень ставим только
            # когда заказ действительно переносят на другую дату.
            if day != timezone.localtime(receipt.created_at).date():
                moment = day_to_moment(day)
                receipt.created_at = moment
                changed.append("order_date")
        if not changed:
            return self._fresh_response(receipt)
        receipt.save(update_fields=["title", "client", "created_at", "updated_at"])
        if moment is not None:
            # Списание материала передвигаем следом: дата заказа опорная для
            # ВСЕЙ отчётности, и расход, оставшийся в прежнем месяце, увёл бы
            # складской лист от выручки.
            receipt.inventory_logs.filter(type=InventoryLog.Type.SALE).update(
                happened_at=moment
            )

        AuditLog.record(request.user, f"Правка чека {receipt.order_number}: {', '.join(sorted(changed))}")
        return self._fresh_response(receipt)

    def destroy(self, request, *args, **kwargs):
        """DELETE /receipts/<id>/ — удалить ошибочно заведённый чек целиком.

        Материал возвращается на склад, оплаты снимаются, движения по чеку
        уходят из журнала склада. В журнале ДЕЙСТВИЙ остаётся запись с составом
        удалённого чека — иначе на вопрос «а что там было» ответить нечем.
        """
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Удалять чеки может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        receipt = self.get_object()
        ensure_open(timezone.localtime(receipt.created_at), "Удалить заказ закрытого периода")
        summary = receipt_summary(receipt)
        try:
            delete_receipt(receipt, user=request.user)
        except DeleteRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(request.user, f"Удалён чек {summary}")
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _resolve_inline_client(self, client_data):
        """Resolve the inline `client` payload to `(Client, name_mismatch)`.

        The frontend may submit a client dict even when the phone already
        belongs to an existing client (the cashier typed the phone without
        picking the live-search match). In that case we reuse the existing
        record instead of failing on the unique-phone validator, and we fill in
        the referrer if it was not set yet (a referral is locked once set, and a
        client can never refer themselves).

        Совпадение ищем ПО ЦИФРАМ номера, а не по строке: раньше сравнивалось
        точное написание, и `0555 111 222` заводил второго клиента поверх
        `+996555111222`. Один человек превращался в двух, а его заказы и долг
        расходились по двум карточкам.

        Реферера проверяем той же проверкой, что и в карточке клиента
        (`ClientSerializer.validate_referred_by`): через кассу нельзя замкнуть
        кольцо «А привёл Б, Б привёл А».

        Телефон совпал, а введённое имя — другое (после выравнивания регистра и
        пробелов): заказ всё равно уходит на найденного клиента, но кассир
        должен увидеть, что это, возможно, не тот человек, — второй элемент
        результата.
        """
        phone = (client_data.get("phone") or "").strip()
        referred_by_id = client_data.get("referred_by")
        existing = find_client_by_phone(phone) if phone else None
        if existing:
            if referred_by_id and existing.referred_by_id is None:
                try:
                    referrer = Client.objects.filter(pk=int(referred_by_id)).first()
                except (TypeError, ValueError):
                    referrer = None
                if referrer is not None and referrer.pk != existing.id:
                    try:
                        ClientSerializer(
                            existing, context={"request": self.request}
                        ).validate_referred_by(referrer)
                    except serializers.ValidationError as exc:
                        raise serializers.ValidationError(
                            {"client": {"referred_by": exc.detail}}
                        )
                    existing.referred_by_id = referrer.pk
                    existing.save(update_fields=["referred_by"])
            return existing, _name_differs(existing, client_data)

        client_serializer = ClientSerializer(
            data=client_data, context={"request": self.request}
        )
        client_serializer.is_valid(raise_exception=True)
        return client_serializer.save(), False

    def _replay_response(self, record, *, checkout=True):
        """Повтор запроса с уже занятым ключом: отдаём результат первого раза."""
        if record.receipt_id is None:
            return Response(
                {"detail": "Запрос с этим ключом уже выполнялся, но заказ с тех пор "
                           "удалён. Повторите операцию с новым ключом."},
                status=status.HTTP_409_CONFLICT,
            )
        fresh = self.get_queryset().get(pk=record.receipt_id)
        payload = dict(ReceiptSerializer(fresh, context={"request": self.request}).data)
        if checkout:
            payload["warnings"] = []
            payload["client_name_mismatch"] = False
        payload["idempotent_replay"] = True
        response = Response(payload, status=record.response_status)
        response["Idempotent-Replay"] = "true"
        return response

    @action(detail=False, methods=["post"], url_path="checkout")
    def checkout(self, request):
        """POST /receipts/checkout/ — create a sale (the main selling flow)."""
        serializer = SaleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        forbidden = _price_override_forbidden(data["items"], request.user)
        if forbidden:
            return Response({"detail": forbidden}, status=status.HTTP_403_FORBIDDEN)

        try:
            idem_key = idempotency.key_from(request)
        except idempotency.InvalidKey as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        client = data.get("client_id")
        name_mismatch = False
        if client is None and data.get("client"):
            client, name_mismatch = self._resolve_inline_client(data["client"])

        pricing, refused = _order_pricing(data, client, request.user)
        if refused:
            return Response({"detail": refused[0]}, status=refused[1])
        is_urgent, urgency_percent, discount_percent = pricing

        # Заказ задним числом оформляет только админ: дата заказа — опорная для
        # выручки, прибыли по дням и складского листа, то есть правит деньги уже
        # закрытых месяцев. Складовщик оформляет продажу сегодняшним днём.
        order_date = data.get("order_date")
        too_old = _check_backdate(order_date)
        if too_old:
            return Response({"order_date": [too_old]}, status=status.HTTP_400_BAD_REQUEST)
        if order_date and order_date != timezone.localdate():
            if not request.user.is_admin_role:
                return Response(
                    {"detail": "Оформить заказ задним числом может только администратор."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        # Закрытый период не пускает даже админа: отчёт за тот месяц уже принят.
        ensure_open(order_date or timezone.localdate(), "Оформить заказ этой датой")

        # Долг гасим ВМЕСТЕ с продажей, но список заказов собираем ДО неё:
        # иначе под погашение попал бы и сам новый заказ, и с клиента взяли бы
        # больше, чем он должен. Право то же, что у «Погасить долг» в карточке
        # клиента, — только админ: складовщик деньги за прошлые заказы не берёт.
        pay_debt = bool(data.get("pay_debt")) and client is not None
        if pay_debt and not request.user.is_admin_role:
            return Response(
                {"detail": "Погасить долг клиента может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        debt_ids = (
            [r.id for r in client.receipts.all() if r.debt > 0] if pay_debt else []
        )
        # Долг гасится ТЕМИ ЖЕ деньгами, что принесли за заказ: «Платит сейчас»
        # — всё, что клиент отдал, сначала заказ, остаток в долги. Раньше сумма
        # сверх заказа становилась сдачей, а долги закрывались отдельно и
        # целиком — кассир, вписавший «заказ + долг», получал двойной счёт.
        # Пустая сумма без «Вся сумма» — платить долг нечем; молча закрывать
        # его нельзя, это и есть тот самый двойной счёт наоборот.
        if debt_ids and data.get("amount_paid") is None and not data.get("pay_full"):
            return Response(
                {"detail": "Укажите, сколько принёс клиент: из этой суммы закрывается "
                           "заказ, а остаток гасит долг. Или нажмите «Вся сумма»."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # Ключ повтора занимается в той же транзакции, что и продажа: упала
            # продажа — откатился и ключ, повтор пройдёт как первый.
            with transaction.atomic():
                record = None
                if idem_key:
                    record, replay = idempotency.claim(
                        request.user, "checkout", idem_key,
                        response_status=status.HTTP_201_CREATED,
                    )
                    if replay:
                        return self._replay_response(record)
                receipt = create_sale(
                    client=client,
                    cashier=request.user,
                    payment_method=data["payment_method"],
                    items_data=data["items"],
                    amount_paid=data.get("amount_paid"),
                    pay_full=bool(data.get("pay_full")),
                    use_change=bool(data.get("use_change")),
                    title=data.get("title", ""),
                    created_at=day_to_moment(order_date),
                    pay_debt_ids=debt_ids,
                    is_urgent=is_urgent,
                    urgency_percent=urgency_percent,
                    discount_percent=discount_percent,
                )
                if record is not None:
                    record.receipt = receipt
                    record.save(update_fields=["receipt"])
        except InsufficientStock as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        debt_paid = getattr(receipt, "debt_paid", Decimal("0"))
        debt_error = getattr(receipt, "debt_error", "")
        if debt_paid > 0:
            AuditLog.record(
                request.user,
                f"Долг клиента {client.display_name} погашен с заказом "
                f"{receipt.order_number}: {debt_paid} сом",
            )

        # Send the electronic receipt to the customer's Telegram (if linked).
        if client:
            send_customer_receipt(client, receipt, _receipt_lines_text(receipt))

        AuditLog.record(request.user, f"Оформлен чек {receipt.order_number} на {receipt.total_price} сом")
        payload = ReceiptSerializer(receipt, context={"request": request}).data
        # Предупреждения оформления (сейчас одно: «себестоимость неизвестна») —
        # продажу они не блокируют, но кассир и владелец должны их видеть.
        payload["warnings"] = strip_cost(getattr(receipt, "cost_warnings", []), request.user)
        # Телефон нашёл существующего клиента, а имя в кассе набрали другое.
        payload["client_name_mismatch"] = name_mismatch
        # Погашение долга — не часть чека, но кассир должен увидеть, что с ним
        # стало: сколько ушло на прошлые заказы и не отказал ли сервер.
        if debt_ids:
            payload["debt_paid"] = debt_paid
            if debt_error:
                payload["debt_error"] = debt_error
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def refund(self, request, pk=None):
        """POST /receipts/<id>/refund/ — refund whole receipt or given items."""
        receipt = self.get_object()
        # Возврат датируется ДНЁМ ОФОРМЛЕНИЯ и двигает цифры этого дня, а не
        # месяца заказа. Поэтому и замок проверяем по сегодняшней дате: заказ
        # из закрытого месяца вернуть можно, закрытый отчёт от этого не меняется.
        ensure_open(timezone.localdate(), "Оформить возврат этой датой")
        serializer = RefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            receipt = refund_receipt(
                receipt,
                item_ids=serializer.validated_data.get("item_ids") or None,
                user=request.user,
            )
        except ItemEditRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if receipt.client:
            notify_customer(
                receipt.client,
                f"↩️ Оформлен возврат по чеку №{receipt.order_number}. "
                f"Сумма возврата: {receipt.refunded_amount} сом.",
            )
        AuditLog.record(request.user, f"Возврат по чеку {receipt.order_number}")
        return self._fresh_response(receipt)

    # Деньги и откат оплаты — только админ: складовщик оформляет продажу,
    # но не решает, погашен ли долг и не отменяет принятую оплату.
    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def pay(self, request, pk=None):
        """POST /receipts/<id>/pay/ — принять оплату долга (полную или частичную).

        Увеличивает amount_paid; когда долг погашен — статус становится PAID.
        Принесли больше долга — лишнее записывается СДАЧЕЙ (`keep_change`), а не
        отбрасывается: деньги в кассе, и цех должен их клиенту.
        Необязательные `paid_on` (дата задним числом) и `method` попадают в
        запись оплаты; общая выплата за несколько заказов — в разделе клиентов
        (POST /clients/<id>/pay-debt/).
        """
        receipt = self.get_object()
        # Разбор даты — внутри try: кривая дата это 400, а не пятисотка.
        try:
            paid_on = parse_paid_on(request.data.get("paid_on"))
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        ensure_open(paid_on or timezone.localdate(), "Принять оплату этой датой")
        try:
            idem_key = idempotency.key_from(request)
        except idempotency.InvalidKey as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                record = None
                if idem_key:
                    record, replay = idempotency.claim(
                        request.user, f"pay:{receipt.pk}", idem_key,
                        response_status=status.HTTP_200_OK,
                    )
                    if replay:
                        return self._replay_response(record, checkout=False)
                amount = apply_payment(
                    receipt,
                    parse_amount(request.data.get("amount")),
                    user=request.user,
                    paid_on=paid_on,
                    method=request.data.get("method") or None,
                    keep_change=True,
                )
                if record is not None:
                    record.receipt = receipt
                    record.save(update_fields=["receipt"])
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        if receipt.client:
            tail = (
                "Долг полностью погашен. Спасибо!"
                if receipt.payment_status == Receipt.PaymentStatus.PAID
                else f"Остаток долга: {receipt.debt} сом."
            )
            notify_customer(
                receipt.client,
                f"💰 Принята оплата {amount} сом по чеку №{receipt.order_number}. {tail}",
            )
        AuditLog.record(request.user, f"Оплата долга по чеку {receipt.order_number}: +{amount} сом")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def unpay(self, request, pk=None):
        """POST /receipts/<id>/unpay/ — откат оплаты: вернуть чек в «Не оплачено».

        Нужно, если оплату приняли по ошибке (например, не по тому чеку). Обнуляет
        принятую оплату и возвращает весь долг; товар/склад не трогаем (он был
        отгружён при продаже). Недоступно для отменённых и возвращённых чеков.
        """
        receipt = self.get_object()
        ensure_open(timezone.localtime(receipt.created_at), "Откатить оплату закрытого периода")
        # Одна транзакция с замком на чек: раньше откат шёл по частям без
        # `atomic`, и два одновременных запроса писали по встречной записи в
        # кассу (UNPAY дважды), а упавший посередине оставлял чек наполовину
        # откатанным.
        with transaction.atomic():
            lock_receipt(receipt)
            return self._unpay_locked(request, receipt)

    def _unpay_locked(self, request, receipt):
        if receipt.status == Receipt.Status.CANCELLED:
            return Response({"detail": "Чек отменён."}, status=status.HTTP_400_BAD_REQUEST)
        if receipt.payment_status in (
            Receipt.PaymentStatus.REFUNDED,
            Receipt.PaymentStatus.PARTIALLY_REFUNDED,
        ):
            return Response(
                {"detail": "По возвращённому чеку откат оплаты недоступен."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Откатывать можно оплаченный чек (в т.ч. старый, где amount_paid=0 —
        # поле появилось позже) или чек с частичной предоплатой.
        if receipt.payment_status != Receipt.PaymentStatus.PAID and receipt.amount_paid <= 0:
            return Response(
                {"detail": "По этому чеку нет принятой оплаты."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        returned = receipt.amount_paid
        # Из кассы уходит всё, что по этому чеку в неё попало: принятые деньги
        # плюс НЕ ВЫДАННАЯ сдача — она физически лежит в ящике и уходит вместе
        # с откатом (см. обнуление `change_due` ниже). Часть, закрытая СДАЧЕЙ
        # с прошлых заказов (`change_applied`), в кассу по этому чеку не
        # приходила — и уходить ей неоткуда. Раньше она списывалась тоже:
        # заказ на 60, закрытый сдачей, после отката уводил кассу в −60, а
        # клиент терял свою сдачу и снова был должен 60 — платил дважды.
        returned_cash = returned - receipt.change_applied + receipt.change_due
        own_change = receipt.change_due
        # Сдача клиента возвращается ему — на другой его заказ, а если других
        # нет, то сдачей на этот же (`return_applied_change`).
        return_applied_change(receipt)
        receipt.refresh_from_db()
        receipt.amount_paid = Decimal("0")
        # Сдача С ЭТОЙ оплаты уходит вместе с ней: откат означает «денег не
        # брали», а сдача — часть тех же денег. Оставить её значило бы, что цех
        # должен клиенту сдачу с платежа, которого не было. Возвращённая выше
        # сдача прошлых заказов остаётся.
        receipt.change_due = receipt.change_due - own_change
        receipt.payment_status = Receipt.PaymentStatus.PENDING
        receipt.save(
            update_fields=["amount_paid", "change_due", "payment_status", "updated_at"]
        )
        # Записи оплат тоже убираем: откат означает «денег не брали», а
        # оставшаяся запись показывала бы в истории клиента платёж, которого нет.
        receipt.payments.all().delete()
        # А вот кассовую книгу не подчищаем — пишем встречный расход: по ней
        # должно быть видно, что деньги приходили и их откатили, иначе остаток
        # сойдётся, а объяснить его будет нечем.
        cash.payment_reverted(receipt, returned_cash, user=request.user)

        AuditLog.record(request.user, f"Откат оплаты по чеку {receipt.order_number}: −{returned} сом")
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], url_path="edit-items", permission_classes=[IsAdmin])
    def edit_items(self, request, pk=None):
        """POST /receipts/<id>/edit-items/ — править состав чека.

        Тело: `items` — список `{id, quantity?, price_per_item?, remove?}`.
        Склад, себестоимость, итог, статус оплаты и сдача пересчитываются;
        если итог упал ниже уже принятых денег, разница становится сдачей.
        """
        receipt = self.get_object()
        ensure_open(timezone.localtime(receipt.created_at), "Править состав заказа закрытого периода")
        changes = request.data.get("items")
        if not isinstance(changes, list) or not changes:
            return Response(
                {"detail": "Не передано ни одной правки."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        before = receipt_summary(receipt)
        try:
            update_receipt_items(receipt, changes, user=request.user)
        except ItemEditRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except InsufficientStock as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        receipt.refresh_from_db()
        AuditLog.record(
            request.user,
            f"Правка состава чека {receipt.order_number}: было {before} → стало "
            f"{receipt_summary(receipt)}",
        )
        response = self._fresh_response(receipt)
        response.data["warnings"] = strip_cost(getattr(receipt, "cost_warnings", []), request.user)
        return response

    @action(detail=True, methods=["post"], url_path="give-change", permission_classes=[IsAdmin])
    def give_change_action(self, request, pk=None):
        """POST /receipts/<id>/give-change/ — отдать клиенту сдачу.

        Пустая сумма — отдать всю. Частично можно: мелочи в кассе может не
        хватить и во второй раз, «отдал тысячу из полутора» — рабочая ситуация.

        ЗАКРЫТЫЙ ПЕРИОД выдаче не мешает — как не мешает и приёму долга по
        старому заказу. Клиент приходит за своей сдачей когда придёт, деньги
        уходят из кассы СЕГОДНЯ (`cash.change_given` датируется сегодняшним
        днём), выручка и прибыль закрытого месяца не двигаются. Проверка тут
        стояла и запирала чужие деньги: «закрыли июль — сдачу за июль не
        отдать», и единственным выходом было открыть весь месяц.
        """
        receipt = self.get_object()
        try:
            given = give_change(receipt, parse_amount(request.data.get("amount")), user=request.user)
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        AuditLog.record(
            request.user,
            f"Выдана сдача по чеку {receipt.order_number}: −{given} сом "
            f"(осталось {receipt.change_due})",
        )
        if receipt.client and receipt.change_due <= 0:
            notify_customer(
                receipt.client,
                f"💵 Вам выдана сдача {given} сом по заказу №{receipt.order_number}.",
            )
        return self._fresh_response(receipt)

    @action(detail=True, methods=["post"], url_path="add-items")
    def add_items(self, request, pk=None):
        """POST /receipts/<id>/add-items/ — дозаказ: add lines to an open order."""
        receipt = self.get_object()
        # Дозаказ поднимает сумму чека и списывает склад — для закрытого месяца
        # это такая же правка задним числом, как исправление состава. Замок
        # держал правку и удаление, а эту дверь не закрывал: заказ 10 августа
        # спокойно вырастал с 200 до 250 сом уже после закрытия месяца.
        ensure_open(
            timezone.localtime(receipt.created_at),
            "Дозаказать в заказ закрытого периода",
        )
        serializer = SaleItemInputSerializer(data=request.data.get("items", []), many=True)
        serializer.is_valid(raise_exception=True)
        forbidden = _price_override_forbidden(serializer.validated_data, request.user)
        if forbidden:
            return Response({"detail": forbidden}, status=status.HTTP_403_FORBIDDEN)
        try:
            receipt, surcharge = add_items_to_receipt(
                receipt, serializer.validated_data, user=request.user
            )
        except (OrderClosed, InsufficientStock) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if receipt.client and surcharge:
            notify_customer(
                receipt.client,
                f"➕ В ваш заказ №{receipt.order_number} добавлены позиции на {surcharge} сом. "
                f"Новый итог: {receipt.total_price} сом.",
            )
        AuditLog.record(request.user, f"Дозаказ по чеку {receipt.order_number}: +{surcharge} сом")
        response = self._fresh_response(receipt)
        response.data["warnings"] = strip_cost(getattr(receipt, "cost_warnings", []), request.user)
        return response

    # --- Статусы выполнения ----------------------------------------------
    #
    # Раньше обе ручки просто присваивали поле и слали уведомление — без единой
    # проверки. Отменённый и полностью возвращённый заказ спокойно становился
    # «готов к выдаче», выданный откатывался обратно в «готов», а клиенту при
    # этом уходило «✅ ваш заказ выполнен и ждёт вас на складе» — по заказу, за
    # который ему уже вернули деньги. Соседние операции над тем же чеком
    # (дозаказ, повторный возврат, откат оплаты) закрыты, а эти две — нет.
    def _ensure_fulfillable(self, receipt):
        """Заказа больше нет — двигать его по производству нечего.

        Частичный возврат СЮДА НЕ ВХОДИТ: в таком заказе остались живые
        позиции, их всё ещё режут и выдают.
        """
        if (
            receipt.status == Receipt.Status.CANCELLED
            or receipt.payment_status == Receipt.PaymentStatus.REFUNDED
        ):
            raise ItemEditRejected(
                "Заказ отменён и возвращён — статус выполнения не меняется."
            )

    def _set_fulfillment(self, request, receipt, status_value):
        """Проставить статус, уведомить клиента и записать в журнал — один раз.

        Повторный вызов возвращает 200 и НЕ шлёт уведомление второй раз: статус
        уже такой, менять нечего, а клиент не должен получать «ваш заказ готов»
        на каждое нажатие кнопки.

        Ход разрешён в любую сторону: кнопку нажимают руками и промахиваются по
        ней так же, как по любой другой. Раньше «Выдан» был тупиком — промах по
        нему нельзя было исправить ничем, кроме удаления заказа, и цех оставался
        с заказом, который числится у клиента, а лежит на полке.
        """
        was = receipt.fulfillment_status
        if was == status_value:
            return self._fresh_response(receipt)
        receipt.fulfillment_status = status_value
        receipt.save(update_fields=["fulfillment_status", "updated_at"])
        if receipt.client:
            notify_customer(receipt.client, FULFILLMENT_MESSAGES[was, status_value])
        AuditLog.record(
            request.user,
            f"Заказ по чеку {receipt.order_number}: "
            f"«{Receipt.FulfillmentStatus(was).label}» → "
            f"«{Receipt.FulfillmentStatus(status_value).label}»"
            + (" (откат)" if _is_rollback(was, status_value) else ""),
        )
        return self._fresh_response(receipt)

    def _fulfillment_action(self, request, status_value):
        """Общее начало всех трёх ручек: заказ жив — двигаем, отменён — отказ."""
        receipt = self.get_object()
        try:
            self._ensure_fulfillable(receipt)
        except ItemEditRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return self._set_fulfillment(request, receipt, status_value)

    @action(detail=True, methods=["post"], url_path="mark-ready")
    def mark_ready(self, request, pk=None):
        """POST /receipts/<id>/mark-ready/ — service ready, notify customer."""
        return self._fulfillment_action(request, Receipt.FulfillmentStatus.READY)

    @action(detail=True, methods=["post"], url_path="mark-issued")
    def mark_issued(self, request, pk=None):
        """POST /receipts/<id>/mark-issued/ — order handed to the customer.

        Выдать можно и из «готовится»: мелкий заказ отдают сразу, не отмечая
        готовность отдельным нажатием.
        """
        return self._fulfillment_action(request, Receipt.FulfillmentStatus.ISSUED)

    @action(detail=True, methods=["post"], url_path="mark-processing")
    def mark_processing(self, request, pk=None):
        """POST /receipts/<id>/mark-processing/ — вернуть заказ в работу.

        Ручка появилась только ради ошибочного нажатия: «вперёд» заказ идёт сам
        по себе, а назад его двигают, когда готовность отметили раньше времени.
        """
        return self._fulfillment_action(request, Receipt.FulfillmentStatus.PROCESSING)

    @action(detail=True, methods=["post"], url_path="set-fulfillment")
    def set_fulfillment(self, request, pk=None):
        """POST /receipts/<id>/set-fulfillment/ {"status": "..."} — любой статус.

        Одна ручка вместо трёх для интерфейса, где статус выбирают списком, а не
        тремя кнопками. Проверки те же самые.
        """
        wanted = request.data.get("status")
        if wanted not in Receipt.FulfillmentStatus.values:
            allowed = ", ".join(Receipt.FulfillmentStatus.values)
            return Response(
                {"detail": f"Неизвестный статус выполнения. Ожидается один из: {allowed}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return self._fulfillment_action(request, wanted)
