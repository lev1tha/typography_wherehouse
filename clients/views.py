import re
import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import (
    Case,
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    ProtectedError,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower, NullIf
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from finance.periods import ensure_open
from sales import idempotency
from accounts.permissions import IsAdmin, IsAdminOrAccountantRead, IsNotAccountant
from audit.models import AuditLog

from .analytics import annotate_metrics, filter_by_age
from .customer import MIN_PORTAL_PASSWORD
from .merge import MergeRejected, merge_clients, merge_summary
from .models import Client, ClientSettings
from .opening import OpeningRejected, open_debts_qs, pay_opening_debts
from .permissions import CanTakeDebt
from .serializers import (
    ClientDetailSerializer,
    ClientSerializer,
    ClientSettingsSerializer,
    client_debt,
)


def _parse_date(value):
    """'YYYY-MM-DD' → date, иначе None (пустой/битый ввод = без фильтра)."""
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _norm_text(value) -> str:
    """Для поиска: регистр, «ё», знаки препинания и повторные пробелы не мешают."""
    text = (value or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def matching_client_ids(search: str) -> list:
    """Клиенты, подходящие под строку поиска: по имени, компании и по ЦИФРАМ телефона.

    Название сравниваем без знаков препинания («Ак-Жол» = «ак жол»). Телефон —
    по цифрам, как привыкли диктовать: «0555 11 22 33», «+996 555…», «555 11 22».
    Полный номер (9 цифр и больше) сверяем по хвосту `phone_key`, как это делает
    касса при заведении клиента; часть номера — подстрока цифр (код страны и
    ведущий ноль отбрасываем).
    """
    from .phones import LOCAL_LENGTH, only_digits, phone_key

    text = _norm_text(search)
    raw = search.lower()
    looks_like_phone = bool(re.fullmatch(r"[\d\s+()\-]+", search))
    digits = only_digits(search) if looks_like_phone else ""
    variants = set()
    if len(digits) >= 3:
        variants.add(digits)
        if digits.startswith("996"):
            variants.add(digits[3:])
        if digits.startswith("0"):
            variants.add(digits.lstrip("0"))
        variants = {v for v in variants if len(v) >= 3}
    key = phone_key(search) if len(digits) >= LOCAL_LENGTH else ""

    ids = []
    for c in Client.objects.only("id", "full_name", "company_name", "phone"):
        phone = c.phone or ""
        hit = (
            (text and (text in _norm_text(c.full_name) or text in _norm_text(c.company_name)))
            or raw in phone.lower()
        )
        if not hit and (variants or key):
            stored = only_digits(phone)
            hit = bool(key and phone_key(phone) == key) or any(v in stored for v in variants)
        if hit:
            ids.append(c.id)
    return ids


def _num(value) -> str:
    """Число для журнала: без хвоста «.00», запятая вместо точки; пусто — «не задан»."""
    if value is None:
        return "не задан"
    return f"{Decimal(value).normalize():f}".replace(".", ",")


def _money(value) -> str:
    """Сумма для журнала (RU-N22, D-189): «12 000,50» — разряды, запятая, без «,00»;
    пусто — «не задан». Единицы («сом») пишет сам текст записи."""
    if value is None:
        return "не задан"
    from finance.auditing import fmt

    return fmt(Decimal(str(value)))


def _int_param(value):
    """Целое из параметра запроса; пусто и мусор — None (фильтр не применяется)."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class ClientViewSet(viewsets.ModelViewSet):
    """CRM. Live ?search= lookup (по имени, телефону или НОМЕРУ ЗАКАЗА) для
    быстрого автозаполнения в кассе. Поиск регистронезависимый на любой БД
    (фильтрация в Python — SQLite не умеет регистронезависимый LIKE для кириллицы).

    Фильтр периода ?date_from=&date_to= оставляет клиентов, у которых были заказы
    в эти дни; «Заказов» тогда считается за тот же период. Сортировка по клику —
    ?ordering=orders_count|-orders_count|debt|-debt|sort_name.
    """

    queryset = Client.objects.all()
    # Бухгалтер карточки клиентов не заводит и не правит — он проверяющий.
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filterset_fields = ["type"]
    # search_fields НЕ задаём: DRF SearchFilter использует icontains, который на
    # SQLite не находит кириллицу в другом регистре. Ищем сами в get_queryset.
    ordering = ["sort_name"]
    ordering_fields = [
        "sort_name", "orders_count", "debt", "change_due_total", "created_at",
        "balance", "advance_total", "last_order_at", "margin_total",
    ]
    # Сортировки по давности: «overdue_days» — самые давние долги сверху (это
    # возрастание ДАТЫ самого старого неоплаченного заказа), без долга — внизу.
    AGE_ORDERINGS = {"overdue_days", "-overdue_days", "oldest_debt", "-oldest_debt"}

    def _period(self):
        return (
            _parse_date(self.request.query_params.get("date_from")),
            _parse_date(self.request.query_params.get("date_to")),
        )

    def get_serializer_context(self):
        # Карточка клиента показывает заказы за тот же период, что и список.
        ctx = super().get_serializer_context()
        d_from, d_to = self._period()
        ctx["date_from"], ctx["date_to"] = d_from, d_to
        return ctx

    def get_queryset(self):
        from sales.models import Receipt

        # Сортировка по ИМЕНИ (компания или ФИО), с откатом на телефон — не по дате.
        # На карточке (retrieve) грузим и позиции чеков — для списка заказов клиента.
        prefetch = ["receipts"]
        if self.action in ("export", "aging"):
            prefetch = []
        if self.action == "retrieve":
            prefetch = [
                "receipts__items__material",
                "receipts__items__service",
                "referrals",
            ]

        d_from, d_to = self._period()
        live = ~Q(receipts__status=Receipt.Status.CANCELLED)
        in_period = Q()
        if d_from:
            in_period &= Q(receipts__created_at__date__gte=d_from)
        if d_to:
            in_period &= Q(receipts__created_at__date__lte=d_to)

        # Долг — «на сейчас», период его не двигает: клиент должен независимо от
        # того, за какой месяц мы смотрим заказы. Формула та же, что в
        # Receipt.debt и в сортировке чеков.
        debt_case = Case(
            When(
                Q(receipts__payment_status__in=Receipt.OWING_STATUSES)
                & ~Q(receipts__status=Receipt.Status.CANCELLED)
                & Q(receipts__revenue_recognized_at__isnull=False)
                & Q(
                    receipts__total_price__gt=F("receipts__amount_paid")
                    + F("receipts__refunded_amount")
                ),
                then=F("receipts__total_price")
                - F("receipts__amount_paid")
                - F("receipts__refunded_amount"),
            ),
            default=Value(Decimal("0")),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )

        qs = (
            Client.objects.annotate(
                sort_name=Lower(
                    Coalesce(NullIf("company_name", Value("")), NullIf("full_name", Value("")), "phone")
                ),
                # Сколько клиентов он привёл — подзапросом, а не join'ом: join по
                # `referrals` размножил бы строки, и суммы долга/сдачи ниже
                # умножились бы на число рефералов.
                referrals_total=Coalesce(
                    Subquery(
                        Client.objects.filter(referred_by=OuterRef("pk"))
                        .order_by()
                        .values("referred_by")
                        .annotate(n=Count("pk"))
                        .values("n")
                    ),
                    Value(0),
                ),
                # distinct — иначе join по позициям чеков посчитал бы заказы по разу
                # на каждую строку чека.
                orders_count=Count("receipts", filter=live & in_period, distinct=True),
                receipts_debt=Coalesce(
                    Sum(debt_case),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                # Входящий долг на дату переезда из Excel (волна 2) — подзапросом,
                # а не join'ом: join размножил бы суммы по чекам.
                opening_debt_total=Coalesce(
                    Subquery(
                        open_debts_qs().filter(client=OuterRef("pk")).order_by()
                        .values("client").annotate(v=Sum("remaining")).values("v")
                    ),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
                # Сдача — зеркало долга: сколько ЦЕХ должен клиенту. Считается
                # так же, «на сейчас», и период её не режет: деньги лежат в
                # кассе независимо от того, какой месяц выбран в фильтре.
                # Возвращённые целиком заказы НЕ отсекаем: деньги по ним лежат
                # в кассе, а сдачу зачёт в новый заказ видит (sale_service
                # `client_change_available`) — одно правило на все места.
                change_due_total=Coalesce(
                    Sum("receipts__change_due"),
                    Value(Decimal("0")),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
            )
            .annotate(
                # Долг клиента = долг по чекам + входящий долг — та же сумма, что
                # `client_debt` карточки (одна формула, D-91).
                debt=ExpressionWrapper(
                    F("receipts_debt") + F("opening_debt_total"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                ),
            )
            .select_related("referred_by")
            .prefetch_related(*prefetch)
            .order_by("sort_name")
        )

        # Период сужает СПИСОК клиентов через отдельный подзапрос, а не через тот
        # же join — иначе он обрезал бы и долг, который должен быть «на сейчас».
        # Только список: карточка за день без заказов — это пустой список
        # заказов, а не «клиент не найден» (404).
        if (d_from or d_to) and self.action == "list":
            recent = Receipt.objects.filter(client=OuterRef("pk")).exclude(
                status=Receipt.Status.CANCELLED
            )
            if d_from:
                recent = recent.filter(created_at__date__gte=d_from)
            if d_to:
                recent = recent.filter(created_at__date__lte=d_to)
            qs = qs.filter(Exists(recent))

        # Фильтр «только должники» — по той же аннотации, что и сортировка.
        if self.request.query_params.get("has_debt") in ("1", "true", "True"):
            qs = qs.filter(debt__gt=0)

        qs = annotate_metrics(qs)

        # «Кому мы должны» — обратный список к должникам: сдача или аванс.
        if self.request.query_params.get("has_change") in ("1", "true", "True"):
            qs = qs.filter(Q(change_due_total__gt=0) | Q(advance_total__gt=0))

        # Давность долга: «должен не меньше N дней» и корзина «от — до».
        params = self.request.query_params
        qs = filter_by_age(
            qs,
            overdue_days=_int_param(params.get("overdue_days")),
            age_from=_int_param(params.get("age_from")),
            age_to=_int_param(params.get("age_to")),
        )

        # «Спящие»: были заказы, но последний — раньше, чем N дней назад.
        sleeping = _int_param(params.get("sleeping_days"))
        if sleeping is not None and sleeping >= 0:
            cutoff = timezone.make_aware(
                datetime.combine(timezone.localdate() - timedelta(days=sleeping), time.min)
            )
            qs = qs.filter(last_order_at__isnull=False, last_order_at__lt=cutoff)

        # Фильтр «заказов от N». Кривое значение игнорируем, а не роняем список.
        try:
            min_orders = int(self.request.query_params.get("min_orders") or 0)
        except (TypeError, ValueError):
            min_orders = 0
        if min_orders > 0:
            qs = qs.filter(orders_count__gte=min_orders)

        search = (self.request.query_params.get("search") or "").strip()
        if search:
            ids = matching_client_ids(search)
            # Поиск по номеру чека: «5» находит клиента, у которого заказ №5.
            if search.lstrip("№").isdigit():
                ids += list(
                    Receipt.objects.filter(order_number=int(search.lstrip("№")))
                    .exclude(client__isnull=True)
                    .values_list("client_id", flat=True)
                )
            qs = qs.filter(id__in=set(ids))
        return qs

    def filter_queryset(self, queryset):
        order = (self.request.query_params.get("ordering") or "").strip()
        # Маржа — закупочная цифра: складовщик по ней не сортирует (иначе по
        # порядку строк он восстановил бы, кто приносит больше денег).
        if order.lstrip("-") == "margin_total" and not self.request.user.sees_money:
            order = ""
            queryset = queryset.order_by("sort_name")
        queryset = super().filter_queryset(queryset)
        if order in self.AGE_ORDERINGS:
            date_field = F("oldest_debt_at")
            oldest_first = order in ("-overdue_days", "oldest_debt")
            queryset = queryset.order_by(
                date_field.asc(nulls_last=True) if oldest_first else date_field.desc(nulls_last=True),
                "sort_name",
            )
        return queryset

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ClientDetailSerializer
        return ClientSerializer

    @action(detail=False, methods=["get"])
    def export(self, request):
        """GET /clients/export/ — CSV списка с теми же фильтрами и сортировкой
        (все страницы сразу). `?has_debt=1` — выгрузка должников."""
        from .export import clients_csv

        queryset = self.filter_queryset(self.get_queryset())
        return clients_csv(queryset, with_margin=request.user.sees_money)

    @action(detail=False, methods=["get"])
    def aging(self, request):
        """GET /clients/aging/ — долг по возрасту заказа: 0–30 / 31–60 / 61–90 / >90."""
        from .analytics import aging_buckets
        from .statement import _plain

        return Response(_plain(aging_buckets()))

    def perform_update(self, serializer):
        """Правки, которые двигают деньги или вход клиента, пишем в журнал
        «было → стало» (XL-07/F3): скидка, лимит долга, телефон, реферер.

        Смена телефона — это ещё и смена логина кабинета; токены клиента при
        этом отзывает сериализатор (версия учётных данных).
        """
        from .phones import phone_key

        old = serializer.instance
        before = {
            "phone": old.phone, "discount": old.discount_percent,
            "limit": old.credit_limit, "referrer": old.referred_by,
        }
        client = serializer.save()
        who, name = self.request.user, client.display_name
        if client.discount_percent != before["discount"]:
            AuditLog.record(
                who,
                f"Изменена скидка клиента «{name}»: "
                f"{_num(before['discount'])} → {_num(client.discount_percent)}%",
            )
        if client.credit_limit != before["limit"]:
            AuditLog.record(
                who,
                f"Изменён лимит долга клиента «{name}»: "
                f"{_money(before['limit'])} → {_money(client.credit_limit)}" + (" сом" if client.credit_limit is not None else ""),
            )
        if phone_key(client.phone) != phone_key(before["phone"]):
            AuditLog.record(who, f"Изменён телефон клиента «{name}»: {before['phone']} → {client.phone}")
        if (client.referred_by_id or None) != (before["referrer"].pk if before["referrer"] else None):
            AuditLog.record(
                who,
                f"Изменён реферер клиента «{name}»: "
                f"{before['referrer'].display_name if before['referrer'] else 'нет'} → "
                f"{client.referred_by.display_name if client.referred_by else 'нет'}",
            )

    def destroy(self, request, *args, **kwargs):
        """Удалить карточку — только пока по ней ничего не проходило.

        `Receipt.client` стоит на `PROTECT`, и это правильно: заказы клиента
        нельзя осиротить. Но необработанный `ProtectedError` отдавал пятисотку и
        страницу отладки — защита данных выглядела поломкой системы. Отвечаем
        по-человечески и показываем на готовый выход: карточки объединяют, а не
        удаляют.
        """
        client = self.get_object()
        orders = client.receipts.count()
        if orders:
            return Response(
                {
                    "detail": (
                        f"У клиента {orders} заказ(ов) — карточку с историей "
                        "удалить нельзя. Если это дубль, объедините его с "
                        "основной карточкой: заказы, оплаты и долг переедут туда."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            # Подстраховка на случай других защищённых ссылок, которые появятся
            # позже: лучше внятный отказ, чем пятисотка.
            return Response(
                {"detail": "По этому клиенту есть записи — карточку удалить нельзя."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    @action(detail=True, methods=["post"], url_path="set-password", permission_classes=[IsAdmin])
    def set_password(self, request, pk=None):
        """Выдать клиенту пароль от кабинета. Только администратор.

        Пароль можно передать в `password`, иначе генерируем. Возвращаем его
        ОДИН раз — в базе лежит только хеш, посмотреть повторно нельзя, можно
        лишь выдать новый. Сюда же сводится и «клиент забыл пароль».
        """
        client = self.get_object()
        raw = (request.data.get("password") or "").strip()
        if raw and len(raw) < MIN_PORTAL_PASSWORD:
            return Response(
                {"detail": f"Пароль минимум {MIN_PORTAL_PASSWORD} символа."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not raw:
            # 6 цифр: админ диктует пароль голосом, буквы и регистр тут только
            # мешают. secrets, а не random — пароль всё-таки.
            raw = f"{secrets.randbelow(1_000_000):06d}"
        client.set_password(raw)
        client.save(update_fields=["portal_password", "credentials_version"])
        AuditLog.record(request.user, f"Выдан пароль кабинета клиенту «{client.display_name}»")
        return Response({"password": raw})

    @action(detail=True, methods=["get"], url_path="merge-preview", permission_classes=[IsAdmin])
    def merge_preview(self, request, pk=None):
        """GET /clients/<id>/merge-preview/?from=<id> — что переедет при склейке.

        Удаление карточки необратимо, поэтому объём показываем ДО подтверждения.
        """
        keep = self.get_object()
        drop = Client.objects.filter(pk=request.query_params.get("from")).first()
        if drop is None:
            return Response({"detail": "Карточка не найдена."}, status=status.HTTP_404_NOT_FOUND)
        if drop.pk == keep.pk:
            return Response(
                {"detail": "Нельзя объединить карточку с ней же."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(merge_summary(keep, drop))

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def merge(self, request, pk=None):
        """POST /clients/<id>/merge/ {"from": <id>} — склеить двойников.

        Один человек, заведённый дважды (номер записали в разном формате), имел
        две карточки, и его заказы с долгом лежали двумя стопками. Всё
        переезжает на ЭТУ карточку, вторая удаляется.

        Необратимо и трогает чужие заказы — поэтому только админ.
        """
        keep = self.get_object()
        drop = Client.objects.filter(pk=request.data.get("from")).first()
        if drop is None:
            return Response({"detail": "Карточка не найдена."}, status=status.HTTP_404_NOT_FOUND)

        summary = merge_summary(keep, drop)
        try:
            keep = merge_clients(keep, drop, user=request.user)
        except MergeRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        AuditLog.record(
            request.user,
            f"Объединены карточки клиентов: «{summary['drop']}» ({summary['drop_phone']}) "
            f"→ «{summary['keep']}»; перенесено заказов: {summary['orders']}",
        )
        return Response(
            self.get_serializer(keep).data | {"merged": summary}
        )

    @action(detail=True, methods=["get"], url_path="credit-check")
    def credit_check(self, request, pk=None):
        """GET /clients/<id>/credit-check/?amount=N — что скажет касса, если
        отгрузить клиенту ещё на N сом в долг: долг, действующий лимит и
        предупреждения `debt_over_limit` (CLI-03)."""
        from .credit import client_debt_now, credit_warnings

        client = get_object_or_404(Client, pk=pk)
        raw = request.query_params.get("amount") or "0"
        try:
            amount = Decimal(raw)
            if not amount.is_finite() or amount < 0:
                raise ValueError
        except (ArithmeticError, ValueError):
            return Response({"detail": "Некорректная сумма."}, status=status.HTTP_400_BAD_REQUEST)
        limit = client.effective_credit_limit
        return Response({
            "debt": client_debt_now(client),
            "limit": limit,
            "limit_source": None if limit is None else ("client" if client.credit_limit is not None else "default"),
            "warnings": credit_warnings(client, amount),
        })

    @action(detail=True, methods=["post"], url_path=r"referral-bonus/(?P<op>pay|unpay)", permission_classes=[IsAdmin])
    def referral_bonus(self, request, pk=None, op=None):
        """POST /clients/<реферер>/referral-bonus/pay|unpay/ {referred, amount?, paid_on?}.

        Записать выплату бонуса за приведённого клиента (всю или часть) или снять
        ошибочную отметку. Деньги не двигает: бонус справочный (D-95).
        """
        from sales.sale_service import PaymentRejected, parse_paid_on

        from .referrals import BonusRejected, pay_bonus, unpay_bonus

        referrer = get_object_or_404(Client, pk=pk)
        referred = get_object_or_404(Client, pk=request.data.get("referred") or 0)
        try:
            if op == "pay":
                paid_on = parse_paid_on(request.data.get("paid_on"))
                row, value = pay_bonus(
                    referrer, referred, amount=request.data.get("amount"),
                    paid_on=paid_on, user=request.user,
                )
                AuditLog.record(
                    request.user,
                    f"Выплачен реферальный бонус клиенту «{referrer.display_name}» за "
                    f"«{referred.display_name}»: {_money(value)} сом",
                )
            else:
                row = unpay_bonus(referrer, referred)
                AuditLog.record(
                    request.user,
                    f"Снята отметка о выплате реферального бонуса «{referrer.display_name}» за «{referred.display_name}»",
                )
        except (BonusRejected, PaymentRejected) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "id": row.id, "amount": row.amount, "paid_amount": row.paid_amount,
            "paid_on": row.paid_on, "due": row.amount - row.paid_amount,
        })

    @action(detail=True, methods=["get"])
    def statement(self, request, pk=None):
        """GET /clients/<id>/statement/?date_from=&date_to= — акт сверки (CLI-04).

        Входящее сальдо — всё до `date_from`, дальше строки периода, обороты и
        исходящее сальдо со знаком: плюс — долг клиента, минус — аванс клиента
        (мы должны). Без дат — вся история, и исходящее сальдо равно карточке.
        """
        from .statement import statement_payload

        client = get_object_or_404(Client, pk=pk)
        try:
            d_from = date.fromisoformat(request.query_params["date_from"]) if request.query_params.get("date_from") else None
            d_to = date.fromisoformat(request.query_params["date_to"]) if request.query_params.get("date_to") else None
        except ValueError:
            return Response({"detail": "Некорректная дата."}, status=status.HTTP_400_BAD_REQUEST)
        if d_from and d_to and d_from > d_to:
            return Response({"detail": "Начало периода позже конца."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(statement_payload(client, d_from, d_to))

    # Деньги принимает админ; складовщик — если владелец это включил (CLI-08).
    @action(detail=True, methods=["post"], url_path="pay-debt", permission_classes=[CanTakeDebt])
    def pay_debt(self, request, pk=None):
        """POST /clients/<id>/pay-debt/ — общая выплата за несколько заказов.

        Клиент приходит и отдаёт деньги «за всё», а не по одному чеку: одна
        сумма гасит долги его заказов от старых к новым. Раньше это приходилось
        разносить руками, открывая каждый заказ отдельно.

        Тело: `amount` (пусто — закрыть выбранные заказы целиком),
        `receipt_ids` (пусто — все заказы с долгом), `paid_on` (можно задним
        числом — только админу), `method` (в том числе `WRITE_OFF` — списание
        долга, только админ), `use_change` — сначала закрыть долг СДАЧЕЙ клиента
        с других заказов и его АВАНСОМ (касса не двигается, CLI-05/cash-08), а
        `amount` — сколько он принёс сверх этого. Возвращает, куда ушли деньги.
        """
        from sales.models import Receipt
        from sales.sale_service import (
            PaymentRejected,
            parse_amount,
            parse_paid_on,
            pay_client_debt,
        )

        from .advances import offset_debts

        client = self.get_object()
        is_admin = request.user.is_admin_role
        # Общая выплата задним числом в закрытый месяц не пускается: она
        # раскидывается по заказам и двигает их долги. Разбор даты оставляем
        # внутри try — кривая дата это тоже 400, а не пятисотка.
        try:
            paid_on = parse_paid_on(request.data.get("paid_on"))
        except PaymentRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if not is_admin and paid_on not in (None, timezone.localdate()):
            return Response(
                {"detail": "Принять оплату прошлой датой может только администратор."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ensure_open(paid_on or timezone.localdate(), "Принять выплату этой датой")

        raw_ids = request.data.get("receipt_ids")
        if raw_ids in (None, "", []):
            receipt_ids = None
        elif isinstance(raw_ids, (list, tuple)):
            receipt_ids = raw_ids
        else:
            return Response(
                {"detail": "Некорректный список заказов."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Входящий долг на дату переезда (волна 2) гасится ПЕРВЫМ: он старше
        # любого заказа системы. Без списка — все входящие долги; в списке они
        # приходят как «opening:<id>» рядом с id заказов.
        opening_ids = None
        if receipt_ids is not None:
            try:
                opening_ids = [
                    int(str(x).split(":", 1)[1]) for x in receipt_ids if str(x).startswith("opening:")
                ]
            except (TypeError, ValueError):
                return Response({"detail": "Некорректный список заказов."}, status=status.HTTP_400_BAD_REQUEST)
            receipt_ids = [x for x in receipt_ids if not str(x).startswith("opening:")]

        method = request.data.get("method") or None
        offset_only = _truthy(request.data.get("offset_only"))
        use_change = _truthy(request.data.get("use_change")) or offset_only
        writing_off = str(method or "").upper() == "WRITE_OFF"
        if use_change and writing_off:
            return Response(
                {"detail": "Списание долга и зачёт сдачи — разные операции."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Лишний ноль (RM-N5, D-164): «37 000 при долге 3 700» — сначала вопрос,
        # как у оплаты одного заказа (`/pay/`). До ключа повтора: вопрос — не
        # операция, подтверждение уйдёт новой попыткой.
        from .amounts import AmountRejected, check_amount

        try:
            # Потолок суммы и тыйыны (RM-N9): телефон в поле суммы или «0,001»
            # — отказ, а не долг на 996 млрд и не тыйын, которого не бывает.
            entered = check_amount(parse_amount(request.data.get("amount")))
        except (PaymentRejected, AmountRejected) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if (
            entered is not None and not writing_off and not offset_only
            and not _truthy(request.data.get("confirm_overpay"))
        ):
            owed = _chosen_debt(client, receipt_ids, opening_ids, use_change=use_change)
            if owed > 0 and entered >= owed * OVERPAY_TIMES:
                return _overpay_response(entered, owed)

        try:
            idem_key = idempotency.key_from(request)
        except idempotency.InvalidKey as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        offset_pairs, via_change, via_advance = [], Decimal("0"), Decimal("0")
        opening_alloc = []
        note = str(request.data.get("note") or "").strip()[:255]
        try:
            with transaction.atomic():
                if idem_key:
                    _record, replay = idempotency.claim(
                        request.user, f"pay-debt:{client.pk}", idem_key,
                        response_status=status.HTTP_200_OK,
                    )
                    if replay:
                        return _replay(
                            "Эта выплата уже принята — повтор запроса ничего не провёл.",
                            {
                                "paid": None, "change": None, "debt": client_debt(Client.objects.get(pk=client.pk)),
                                "offset": None, "allocations": [],
                            },
                        )
                amount = parse_amount(request.data.get("amount"))
                if use_change:
                    offset_pairs, via_change, via_advance = offset_debts(
                        client, receipt_ids=receipt_ids, user=request.user, paid_on=paid_on,
                    )
                if offset_only:
                    # «Только зачесть»: деньги не принимаем, остаток долга остаётся.
                    if not offset_pairs:
                        raise PaymentRejected("Нет сдачи и аванса, которые можно зачесть в долг.")
                    if amount:
                        raise PaymentRejected("Зачёт и приём денег — разные операции.")
                    allocations, change = [], Decimal("0")
                else:
                    if (opening_ids is None or opening_ids) and open_debts_qs().filter(client=client).exists():
                        opening_alloc, rest = pay_opening_debts(
                            client, amount, ids=opening_ids or None, user=request.user,
                            paid_on=paid_on, method=method, note=note,
                        )
                        if amount is not None:
                            amount = rest
                    ids = receipt_ids
                    # Свежие чеки, а не prefetch карточки: он снят до зачёта.
                    wanted = None if receipt_ids is None else {str(x) for x in receipt_ids}
                    owing = [
                        str(r.id) for r in Receipt.objects.filter(client=client)
                        if r.debt > 0 and (wanted is None or str(r.id) in wanted)
                    ]
                    if offset_pairs:
                        ids = owing
                    if not owing and (offset_pairs or opening_alloc or ids == []):
                        if amount:
                            raise PaymentRejected(
                                "Долг уже закрыт зачётом сдачи и аванса — принимать деньги не за что."
                                if offset_pairs else
                                "Сумма больше выбранного долга — лишнее примите авансом."
                            )
                        allocations, change = [], Decimal("0")
                    else:
                        allocations, change = pay_client_debt(
                            client, amount, receipt_ids=ids, user=request.user,
                            paid_on=paid_on, method=method, note=note,
                        )
        except (PaymentRejected, OpeningRejected) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        paid = sum((a for _, a in allocations), Decimal("0")) + sum((a for _, a in opening_alloc), Decimal("0"))
        offset_total = via_change + via_advance
        ordered = ["входящий долг"] if opening_alloc else []
        for r, _a in [*offset_pairs, *allocations]:
            if f"№{r.order_number}" not in ordered:
                ordered.append(f"№{r.order_number}")
        numbers = ", ".join(ordered)
        who = "" if is_admin else f" (принял складовщик {request.user.username})"
        written_off = str(method or "").upper() == "WRITE_OFF"
        what = f"списан долг {_money(paid)} сом" if written_off else f"+{_money(paid)} сом"
        reason = str(request.data.get("note") or "").strip()
        if written_off and reason:
            what += f" — {reason[:120]}"
        if offset_total:
            what += (
                f", зачтено {_money(offset_total)} сом (сдача {_money(via_change)}, "
                f"аванс {_money(via_advance)})"
            )
        AuditLog.record(
            request.user,
            f"{'Списание долга клиента' if written_off else 'Общая выплата клиента'} "
            f"«{client.display_name}»: {what} ({numbers}){who}",
        )

        # Долг пересчитываем СВЕЖИМ запросом: prefetch снят до оплаты и отдал бы
        # старую цифру. Функция та же, что у карточки (чеки + входящий долг).
        left = client_debt(Client.objects.get(pk=client.pk))

        # Одно сообщение на всю выплату, а не по штуке на каждый заказ: клиент
        # заплатил один раз, и пять уведомлений подряд выглядят сбоем.
        if client.telegram_chat_id:
            from integrations.telegram import notify_customer

            tail = (
                "Долгов больше нет. Спасибо!"
                if left <= 0
                else f"Остаток долга: {left} сом."
            )
            notify_customer(
                client,
                f"💰 Принята оплата {paid + offset_total} сом за заказы {numbers}. {tail}",
            )

        def row(r, amount, source):
            return {
                "receipt": str(r.id), "order_number": r.order_number, "title": r.title,
                "amount": amount, "source": source,
                "debt_after": Receipt.objects.get(pk=r.pk).debt,
            }

        return Response(
            {
                "paid": paid,
                # Сдача: принесли больше, чем висело долга. В долг не пишем —
                # лишнее отдают на руки (то же правило, что в кассе).
                "change": change,
                "debt": left,
                "offset": {"change": via_change, "advance": via_advance, "total": offset_total},
                "allocations": [
                    *[
                        {"opening": ob.pk, "order_number": None, "title": "Входящий долг",
                         "amount": a, "source": "opening", "debt_after": ob.remaining}
                        for ob, a in opening_alloc
                    ],
                    *[row(r, a, "offset") for r, a in offset_pairs],
                    *[row(r, a, "cash") for r, a in allocations],
                ],
            }
        )

    @action(detail=True, methods=["get", "post"], url_path="advances", permission_classes=[IsAuthenticated])
    def advances(self, request, pk=None):
        """GET — авансы клиента; POST {amount, method, paid_on?, note?,
        offset_debt?} — принять аванс.

        Деньги принимает тот же круг, что и оплату долга (`CanTakeDebt`).
        `offset_debt` (по умолчанию да, RM-N6/D-165): у клиента есть долг —
        деньги сначала гасят его (входящий долг, потом заказы от старых), авансом
        остаётся только остаток сверх долга. Повтор с тем же `Idempotency-Key`
        ничего не проводит второй раз (CLI-14).
        """
        from .advances import AdvanceRejected, accept_advance_or_offset
        from .models import ClientAdvance
        from sales.sale_service import PaymentRejected, parse_paid_on

        client = get_object_or_404(Client, pk=pk)
        if request.method == "GET":
            return Response(advances_payload(client))
        if not CanTakeDebt().has_permission(request, self):
            return Response(
                {"detail": "Принимать деньги клиента может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            idem_key = idempotency.key_from(request)
        except idempotency.InvalidKey as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        raw_offset = request.data.get("offset_debt")
        offset_debt = True if raw_offset in (None, "") else _truthy(raw_offset)
        try:
            paid_on = parse_paid_on(request.data.get("paid_on"))
            if not request.user.is_admin_role and paid_on not in (None, timezone.localdate()):
                raise AdvanceRejected("Принять аванс прошлой датой может только администратор.")
            ensure_open(paid_on or timezone.localdate(), "Принять аванс этой датой")
            with transaction.atomic():
                if idem_key:
                    record, replay = idempotency.claim(
                        request.user, f"advance:{client.pk}", idem_key,
                        response_status=status.HTTP_201_CREATED,
                    )
                    if replay:
                        first = (
                            ClientAdvance.objects.filter(
                                client=client, created_by=request.user, is_opening=False,
                                created_at__gte=record.created_at,
                                created_at__lte=record.created_at + timedelta(minutes=1),
                            ).order_by("id").first()
                        )
                        fresh = Client.objects.get(pk=client.pk)
                        return _replay(
                            "Этот аванс уже принят — повтор запроса ничего не провёл.",
                            {
                                **(advance_row(first) if first else {"id": None}),
                                "advance": advance_row(first) if first else None,
                                "debt": client_debt(fresh),
                            },
                            code=record.response_status,
                        )
                result = accept_advance_or_offset(
                    client, request.data.get("amount"), method=request.data.get("method") or "CASH",
                    paid_on=paid_on, note=str(request.data.get("note") or ""), user=request.user,
                    offset_debt=offset_debt,
                )
        except (AdvanceRejected, PaymentRejected, OpeningRejected, InvalidOperation, TypeError) as e:
            detail = str(e) if not isinstance(e, (InvalidOperation, TypeError)) else "Некорректная сумма."
            return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)

        advance, to_debt = result["advance"], result["to_debt"]
        accepted = to_debt + (advance.amount if advance else Decimal("0"))
        method_display = dict(ClientAdvance.Method.choices).get(
            str(request.data.get("method") or "CASH").upper(), ""
        )
        what = f"Принят аванс от клиента «{client.display_name}»: {_money(accepted)} сом ({method_display})"
        if to_debt:
            numbers = (["входящий долг"] if result["opening"] else []) + [
                f"№{r.order_number}" for r, _a in result["receipts"]
            ]
            what += f", из них в долг {_money(to_debt)} сом ({', '.join(numbers)}), авансом " + (
                f"{_money(advance.amount)} сом" if advance else "0 сом"
            )
        AuditLog.record(request.user, what)

        fresh = Client.objects.get(pk=client.pk)
        payload = advance_row(advance) if advance else {
            "id": None, "amount": Decimal("0"), "remaining": Decimal("0"),
            "method": str(request.data.get("method") or "CASH").upper(),
            "method_display": method_display, "paid_on": paid_on or timezone.localdate(),
            "note": "", "reverted": False, "used": Decimal("0"),
        }
        return Response(
            {
                # Прежние поля аванса — как раньше (аванса нет — сумма 0).
                **payload,
                "accepted": accepted,
                "to_debt": to_debt,
                "advance": advance_row(advance) if advance else None,
                "debt": client_debt(fresh),
                "allocations": [
                    *[
                        {"opening": ob.pk, "order_number": None, "amount": a, "debt_after": ob.remaining}
                        for ob, a in result["opening"]
                    ],
                    *[
                        {"receipt": str(r.id), "order_number": r.order_number, "amount": a,
                         "debt_after": r.debt}
                        for r, a in result["receipts"]
                    ],
                ],
            },
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True, methods=["post"], url_path=r"advances/(?P<advance_id>\d+)/revert",
        permission_classes=[IsAdmin],
    )
    def revert_advance(self, request, pk=None, advance_id=None):
        """POST /clients/<id>/advances/<advance>/revert/ — отменить ошибочный аванс."""
        from .advances import AdvanceRejected, revert_advance
        from .models import ClientAdvance

        client = get_object_or_404(Client, pk=pk)
        advance = get_object_or_404(ClientAdvance, pk=advance_id, client=client)
        try:
            revert_advance(advance, user=request.user)
        except AdvanceRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Отменён аванс клиента «{client.display_name}»: {_money(advance.amount)} сом",
        )
        advance.refresh_from_db()
        return Response(advance_row(advance))


def _truthy(value) -> bool:
    return value is True or str(value).lower() in ("1", "true", "yes", "on")


# «Общая выплата» в N и больше раз выше выбранного долга — сначала вопрос
# (RM-N5, D-164): лишний ноль (37 000 вместо 3 700) не уходит молча в сдачу.
OVERPAY_TIMES = 3


def _chosen_debt(client, receipt_ids, opening_ids, *, use_change=False) -> Decimal:
    """Долг, который гасит общая выплата: выбранные заказы (или все с долгом) и
    выбранный входящий долг (или весь — если списка нет). С зачётом сдачи и
    аванса — за их вычетом: деньгами закрывается только остаток."""
    from sales.models import Receipt

    from .advances import advance_available

    wanted = None if receipt_ids is None else {str(x) for x in receipt_ids}
    owed = sum(
        (r.debt for r in Receipt.objects.filter(client=client)
         if r.debt > 0 and (wanted is None or str(r.id) in wanted)),
        Decimal("0"),
    )
    if opening_ids is None or opening_ids:
        qs = open_debts_qs().filter(client=client)
        if opening_ids:
            qs = qs.filter(pk__in=opening_ids)
        owed += sum((ob.remaining for ob in qs), Decimal("0"))
    if use_change:
        from sales.sale_service import client_change_available

        owed -= advance_available(client) + client_change_available(client)
    return max(owed, Decimal("0"))


def _overpay_response(amount: Decimal, owed: Decimal) -> Response:
    """409 «точно столько?» — тот же ответ, что у `/pay/` (фронт его уже знает)."""
    warning = {
        "code": "overpay", "amount": amount, "debt": owed,
        "message": (
            f"Вы вводите {amount} сом при долге {owed} сом — больше в "
            f"{(amount / owed).quantize(Decimal('0.1'))} раза. Излишек останется "
            "сдачей клиенту. Всё верно?"
        ),
    }
    return Response(
        {"detail": warning["message"], "needs_confirmation": True, "warnings": [warning]},
        status=status.HTTP_409_CONFLICT,
    )


def _replay(detail: str, payload: dict, code=status.HTTP_200_OK) -> Response:
    """Повтор с уже занятым `Idempotency-Key` (CLI-14): операция не проводится,
    ответ — текущее состояние и пометка повтора (как у `/pay/`)."""
    response = Response({**payload, "detail": detail, "idempotent_replay": True}, status=code)
    response["Idempotent-Replay"] = "true"
    return response


def advance_row(a) -> dict:
    return {
        "id": a.id, "amount": a.amount, "remaining": a.remaining, "method": a.method,
        "method_display": a.get_method_display(), "paid_on": a.paid_on, "note": a.note,
        "reverted": a.reverted_at is not None,
        "used": a.amount - a.remaining if not a.reverted_at else Decimal("0"),
    }


def advances_payload(client) -> list:
    return [advance_row(a) for a in client.advances.order_by("-paid_on", "-id")]


class ClientSettingsView(APIView):
    """GET/PATCH /api/clients/settings/ — общие правила клиентов (D-92).

    Читают все сотрудники, меняет только администратор; каждая правка — в
    журнал «было → стало».
    """

    permission_classes = [IsAuthenticated]

    LABELS = {
        "default_credit_limit": "общий лимит долга",
        "storekeeper_takes_debt": "складовщик принимает оплату долга",
    }

    def get(self, request):
        return Response(ClientSettingsSerializer(ClientSettings.load()).data)

    def patch(self, request):
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Настройки клиентов меняет только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        current = ClientSettings.load()
        before = ClientSettingsSerializer(current).data
        serializer = ClientSettingsSerializer(current, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        after = ClientSettingsSerializer(ClientSettings.load()).data
        parts = []
        for key, label in self.LABELS.items():
            if before[key] != after[key]:
                parts.append(f"{label}: {_show(before[key])} → {_show(after[key])}")
        if parts:
            AuditLog.record(request.user, "Настройки клиентов: " + "; ".join(parts))
        return Response(after)


def _show(value) -> str:
    if isinstance(value, bool):
        return "вкл" if value else "выкл"
    return _num(value)


class OpeningBalanceViewSet(viewsets.ViewSet):
    """Входящие остатки клиентов при переезде из Excel (XL-04/F6/CLI-06, волна 2).

    `GET  /clients/opening-balances/?client=&batch=` — список (админ, бухгалтер);
    `POST /clients/opening-balances/preview/` {text} — что будет сделано по строкам;
    `POST /clients/opening-balances/` {text, as_of, note?} — провести (всё или ничего);
    `POST /clients/opening-balances/<id>/revert/` — отменить ошибочный остаток;
    `POST /clients/opening-balances/<id>/cancel-payment/` {payment, reason?} —
    отменить одну оплату входящего долга (D-141).
    Проводит и отменяет только админ.
    """

    permission_classes = [IsAuthenticated, IsAdminOrAccountantRead]

    def list(self, request):
        from .models import OpeningBalance
        from .opening import balances_payload
        from .statement import _plain

        qs = OpeningBalance.objects.all().order_by("-as_of", "-id")
        client_id = _int_param(request.query_params.get("client"))
        if client_id:
            qs = qs.filter(client_id=client_id)
        batch = (request.query_params.get("batch") or "").strip()
        if batch:
            qs = qs.filter(batch=batch)
        if request.query_params.get("active") in ("1", "true"):
            qs = qs.filter(reverted_at__isnull=True)
        return Response(_plain(balances_payload(qs[:1000])))

    @action(detail=False, methods=["post"])
    def preview(self, request):
        from .opening import OpeningRejected, parse_text, preview
        from .statement import _plain

        try:
            plan = preview(parse_text(str(request.data.get("text") or "")))
        except OpeningRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_plain(plan))

    def create(self, request):
        """Провести вставку. Повтор с тем же `Idempotency-Key` (CLI-14) ничего не
        проводит второй раз и отдаёт ту же партию: её имя выводится из ключа."""
        from .opening import OpeningRejected, balances_payload, batch_for_key, post
        from .models import OpeningBalance
        from .statement import _plain

        as_of = _parse_date(request.data.get("as_of"))
        if as_of is None:
            return Response({"as_of": ["Укажите дату переезда."]}, status=status.HTTP_400_BAD_REQUEST)
        try:
            idem_key = idempotency.key_from(request)
        except idempotency.InvalidKey as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                batch = None
                if idem_key:
                    record, replay = idempotency.claim(
                        request.user, "opening-balances", idem_key,
                        response_status=status.HTTP_201_CREATED,
                    )
                    batch = batch_for_key(record)
                    if replay:
                        made = OpeningBalance.objects.filter(batch=batch)
                        if not made.exists():
                            return Response(
                                {"detail": "Запрос с этим ключом уже выполнялся. Повторите "
                                           "проведение с новым ключом."},
                                status=status.HTTP_409_CONFLICT,
                            )
                        rows = balances_payload(made)
                        return _replay(
                            "Эти остатки уже проведены — повтор запроса ничего не провёл.",
                            _plain({
                                "batch": batch, "created_clients": None,
                                "debt": sum((b.amount for b in made if b.kind == "DEBT"), Decimal("0")),
                                "advance": sum((b.amount for b in made if b.kind == "ADVANCE"), Decimal("0")),
                                "rows": rows,
                            }),
                            code=status.HTTP_201_CREATED,
                        )
                result = post(
                    str(request.data.get("text") or ""), as_of, user=request.user,
                    note=str(request.data.get("note") or ""), batch=batch,
                )
        except OpeningRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Проведены входящие остатки клиентов на {as_of:%d.%m.%Y}: долг {_money(result['debt'])} сом, "
            f"аванс {_money(result['advance'])} сом, строк {len(result['balances'])}, "
            f"новых клиентов {result['created_clients']} (партия {result['batch']})",
            kind="client",
        )
        rows = balances_payload(OpeningBalance.objects.filter(batch=result["batch"]))
        return Response(_plain({
            "batch": result["batch"], "created_clients": result["created_clients"],
            "debt": result["debt"], "advance": result["advance"], "rows": rows,
        }), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def revert(self, request, pk=None):
        from .models import OpeningBalance
        from .opening import OpeningRejected, balances_payload, revert
        from .statement import _plain

        balance = get_object_or_404(OpeningBalance, pk=pk)
        try:
            revert(balance, user=request.user)
        except OpeningRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Отменён входящий {'долг' if balance.kind == 'DEBT' else 'аванс'} клиента "
            f"«{balance.client.display_name}» на {balance.as_of:%d.%m.%Y}: {_money(balance.amount)} сом",
            kind="client",
        )
        return Response(_plain(balances_payload(OpeningBalance.objects.filter(pk=balance.pk))[0]))

    @action(detail=True, methods=["post"], url_path="cancel-payment")
    def cancel_payment(self, request, pk=None):
        """POST /clients/opening-balances/<id>/cancel-payment/ {payment, reason?} —
        отменить ОДНУ оплату входящего долга (D-141, админ).

        Как отмена оплаты заказа: исходный приход остаётся в кассовой книге,
        сегодня пишется встречный расход, в журнале — кто, сколько и почему.
        Замок периода — по СЕГОДНЯШНЕЙ дате: встречная запись ложится сегодня
        и закрытый месяц не меняет (у списания — ещё и месяц списания: его
        расход уходит).
        """
        from .models import OpeningBalance
        from .opening import OpeningRejected, balances_payload, cancel_opening_payment
        from .statement import _plain

        balance = get_object_or_404(OpeningBalance, pk=pk)
        ensure_open(timezone.localdate(), "Отменить оплату этой датой")
        reason = str(request.data.get("reason") or "").strip()[:200]
        try:
            payment = cancel_opening_payment(
                balance, request.data.get("payment"), user=request.user, reason=reason,
            )
        except OpeningRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        what = "списания" if payment.method == "WRITE_OFF" else "оплаты"
        AuditLog.record(
            request.user,
            f"Клиент «{balance.client.display_name}»: отмена {what} входящего долга от "
            f"{payment.paid_on:%d.%m.%Y} — −{_money(payment.amount)} сом ({payment.get_method_display()})"
            + (f" ({reason})" if reason else ""),
            kind="client",
        )
        return Response(_plain(balances_payload(OpeningBalance.objects.filter(pk=balance.pk))[0]))


class ClientPriceViewSet(viewsets.ModelViewSet):
    """Договорные цены клиента (CLI-02, часть; волна 2).

    `GET /clients/client-prices/?client=<id>` — весь персонал (касса видит, по
    какой цене пойдёт строка); создать, поправить, удалить — только админ, с
    записью «было → стало» в журнал.
    """

    pagination_class = None

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsAdmin()]

    def get_serializer_class(self):
        from .serializers import ClientPriceSerializer

        return ClientPriceSerializer

    def get_queryset(self):
        from .models import ClientPrice

        qs = ClientPrice.objects.select_related("client", "service", "material")
        client_id = _int_param(self.request.query_params.get("client"))
        if client_id:
            qs = qs.filter(client_id=client_id)
        return qs

    @staticmethod
    def _what(cp) -> str:
        what = cp.service.name if cp.service_id else ""
        if cp.material_id:
            what = f"{what} · {cp.material.name}" if what else cp.material.name
        if cp.sale_mode:
            what += f" ({cp.get_sale_mode_display().lower()})"
        return what

    def perform_create(self, serializer):
        cp = serializer.save(updated_by=self.request.user)
        AuditLog.record(
            self.request.user,
            f"Договорная цена клиента «{cp.client.display_name}»: {self._what(cp)} — {_money(cp.price)} сом",
            kind="price",
        )

    def perform_update(self, serializer):
        was = serializer.instance.price
        cp = serializer.save(updated_by=self.request.user)
        if was != cp.price:
            AuditLog.record(
                self.request.user,
                f"Изменена договорная цена клиента «{cp.client.display_name}»: {self._what(cp)} — "
                f"{_money(was)} → {_money(cp.price)} сом",
                kind="price",
            )

    def perform_destroy(self, instance):
        AuditLog.record(
            self.request.user,
            f"Удалена договорная цена клиента «{instance.client.display_name}»: "
            f"{self._what(instance)} — {_money(instance.price)} сом",
            kind="price",
        )
        instance.delete()
