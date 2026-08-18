import calendar
import secrets
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core import signing
from django.db.models import Count, DecimalField, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import IsAdminOrAccountantRead, SeesMoney
from audit.models import AuditLog
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import InventoryLog, Material, Roll, Supply

from .material_sheet import (
    collect_flows,
    collect_manual,
    counting_unit,
    opening_for,
    purchases_from_stock,
    purchases_from_stock_by_day,
    q2,
    sheet_unit,
    to_units,
)
from .models import (
    CashEntry,
    CompanyProfile,
    ExpenseEntry,
    ExpenseKind,
    FinanceSettings,
    PeriodLock,
)
from .periods import ensure_open
from .serializers import (
    CashEntrySerializer,
    PeriodLockSerializer,
    CompanyProfileSerializer,
    ExpenseEntrySerializer,
    ExpenseKindSerializer,
    FinanceSettingsSerializer,
)

_SUM = lambda field: Coalesce(Sum(field), Decimal("0"), output_field=DecimalField())


# Сколько держится снятый финансовый пароль. Столько же, сколько держал старый
# признак во фронтенде, — но теперь срок считает сервер, а не браузер.
FINANCE_UNLOCK_TTL = 30 * 60
_finance_signer = signing.TimestampSigner(salt="finance-unlock")


class FinanceUnlockView(APIView):
    """POST /api/finance/unlock/ — verify the separate password that gates the
    Finance & detailed-analytics screens (on top of the login). Админ и бухгалтер;
    the password itself lives in settings (FINANCE_PASSWORD, configured via .env),
    so it never ships in the frontend bundle.

    Снятие пароля подтверждается ПОДПИСАННЫМ признаком с сервера, а не отметкой
    времени в браузере. Раньше фронтенд хранил `financeUnlockedAt` — обычное
    число, — и строка `localStorage.setItem('financeUnlockedAt', Date.now())`
    открывала «Финансы» целиком, не зная пароля. Подделать подпись, не зная
    SECRET_KEY, нельзя, а срок жизни проверяет сервер (`GET`), а не браузер.

    Что API финансов остаётся доступен обычному токену админа и бухгалтера —
    так и задумано: этот пароль отделяет ЭКРАНЫ, а не роли, и того, кто уже
    вошёл админом, он от его же данных не защищает.
    """

    permission_classes = [SeesMoney]

    def post(self, request):
        supplied = str(request.data.get("password") or "")
        expected = str(getattr(settings, "FINANCE_PASSWORD", "") or "")
        # Сравниваем БАЙТЫ, а не строки: `compare_digest` на строках с не-ASCII
        # бросает TypeError, и кириллический финансовый пароль (а цех тут
        # русско- и кыргызоязычный) ронял раздел пятисоткой вместо проверки.
        # Постоянное время сравнения при этом сохраняется.
        if expected and secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            return Response({"ok": True, "token": _finance_signer.sign(str(request.user.pk))})
        return Response({"detail": "Неверный пароль."}, status=status.HTTP_403_FORBIDDEN)

    def get(self, request):
        """Действителен ли ещё признак, выданный этому пользователю."""
        token = request.headers.get("X-Finance-Unlock") or request.query_params.get("token") or ""
        try:
            owner = _finance_signer.unsign(token, max_age=FINANCE_UNLOCK_TTL)
        except (signing.BadSignature, signing.SignatureExpired):
            return Response({"ok": False})
        # Признак именной: разблокировка одного сотрудника не открывает раздел
        # тому, кто сядет за ту же машину следующим.
        return Response({"ok": owner == str(request.user.pk)})


def _parse_date(value):
    """'YYYY-MM-DD' → date, иначе None (пустой/битый ввод = без фильтра)."""
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _filter_by_month(qs, request, field):
    """Месячный фильтр ?year=&month= по указанному полю-дате.

    Оба параметра нужны вместе; кривые значения просто игнорируем, чтобы
    список не падал с 500 из-за опечатки в адресе."""
    try:
        year = int(request.query_params.get("year") or 0)
        month = int(request.query_params.get("month") or 0)
    except (TypeError, ValueError):
        return qs
    if not year or not (1 <= month <= 12):
        return qs
    return qs.filter(**{f"{field}__year": year, f"{field}__month": month})


def _prorate_factor(d_from, d_to):
    """Доля «месяца» в выбранном периоде [d_from, d_to] включительно: за каждый
    день берём 1/дней_в_его_месяце и суммируем. Полный календарный месяц → 1.0,
    половина месяца → ~0.5, период из нескольких месяцев → сумма их долей. Нужна,
    чтобы постоянные (месячные) расходы — аренда/коммуналка/зарплата — за неполный
    период показывались пропорционально, как в дневном графике, а не целиком."""
    factor = Decimal("0")
    y, m = d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        dim = calendar.monthrange(y, m)[1]
        lo = max(d_from, date(y, m, 1))
        hi = min(d_to, date(y, m, dim))
        factor += Decimal((hi - lo).days + 1) / Decimal(dim)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return factor


class ExpenseKindViewSet(viewsets.ModelViewSet):
    """Справочник видов расхода — строк финотчёта. Admin-only.

    Свои виды админ заводит сам («Реклама», «Налоги»); встроенные переименовать
    можно, удалить — нет. Скрытые по умолчанию не отдаются, `?archived=1` их
    показывает (та же логика, что у материалов на складе)."""

    serializer_class = ExpenseKindSerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_fields = ["block", "in_profit"]
    pagination_class = None

    def get_queryset(self):
        qs = ExpenseKind.objects.annotate(entries_total=Count("entries"))
        # «Вернуть» по определению работает со скрытым видом — иначе он не
        # нашёлся бы в отфильтрованном списке и ответ был бы 404.
        if self.action == "restore" or self.request.query_params.get("archived") == "1":
            return qs
        return qs.filter(is_archived=False)

    def destroy(self, request, *args, **kwargs):
        kind = self.get_object()
        if kind.is_builtin:
            return Response(
                {"detail": "Встроенный вид расхода удалить нельзя — его можно скрыть."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Вид с историей не удаляем, а скрываем: иначе суммы прошлых месяцев
        # поехали бы задним числом (и PROTECT всё равно не дал бы удалить).
        if kind.entries.exists():
            kind.is_archived = True
            kind.save(update_fields=["is_archived"])
            return Response({"archived": True}, status=status.HTTP_200_OK)
        kind.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        """Вернуть скрытый вид расхода в отчёт."""
        kind = self.get_object()
        kind.is_archived = False
        kind.save(update_fields=["is_archived"])
        return Response(self.get_serializer(kind).data)


class ExpenseEntryViewSet(viewsets.ModelViewSet):
    """Траты по видам: аренда, зарплата, фреза, транспорт, свои виды.

    Фильтры: ?kind=, ?year=&month= (месяц), ?date_from=&date_to= (период),
    ?spent_at=<дата> (день)."""

    serializer_class = ExpenseEntrySerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_fields = ["kind", "spent_at"]
    ordering = ["-spent_at", "-created_at"]
    # Диалог вида расхода показывает все траты за период целиком — страница на
    # 25 строк молча обрезала бы месяц, и итог в диалоге разошёлся бы с отчётом.
    pagination_class = None

    def get_queryset(self):
        qs = ExpenseEntry.objects.select_related("kind")
        qs = _filter_by_month(qs, self.request, "spent_at")
        d_from = _parse_date(self.request.query_params.get("date_from"))
        d_to = _parse_date(self.request.query_params.get("date_to"))
        if d_from:
            qs = qs.filter(spent_at__gte=d_from)
        if d_to:
            qs = qs.filter(spent_at__lte=d_to)
        return qs

    def perform_create(self, serializer):
        # Трата задним числом в закрытый месяц изменила бы принятый отчёт.
        ensure_open(serializer.validated_data.get("spent_at"), "Записать трату этой датой")
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        ensure_open(serializer.instance.spent_at, "Править трату закрытого периода")
        ensure_open(serializer.validated_data.get("spent_at"), "Перенести трату этой датой")
        serializer.save()

    def perform_destroy(self, instance):
        ensure_open(instance.spent_at, "Удалить трату закрытого периода")
        instance.delete()

    @action(detail=False, methods=["get"])
    def feed(self, request):
        """GET /finance/expense-entries/feed/ — ВСЕ траты периода одной лентой.

        Ручные записи — это лишь часть расхода. Закуп материала система считает
        сама, по приходам на склад (`purchases_from_stock`), и в `ExpenseEntry`
        он не попадает НИКОГДА. Поэтому список «Все траты за период», читавший
        только ручные записи, у заказчика был пуст всегда: все его траты — это
        приходы материала, а в отчёте сверху при этом стояло «Расходы 25 000».

        Здесь обе стороны в одной ленте: ручная запись правится и удаляется,
        приход показывается справочно и ведёт к своей накладной.
        """
        d_from = _parse_date(request.query_params.get("date_from"))
        d_to = _parse_date(request.query_params.get("date_to"))
        rows = [
            {
                "key": f"entry-{e.id}",
                "source": "MANUAL",
                "id": e.id,
                "kind": e.kind_id,
                "kind_name": e.kind.name,
                "name": e.name,
                "amount": e.amount,
                "spent_at": e.spent_at,
                "note": e.note,
            }
            for e in self.filter_queryset(self.get_queryset())
        ]

        purchase_kind = ExpenseKind.objects.filter(
            code=ExpenseKind.MATERIAL_PURCHASE
        ).first()
        purchase_name = purchase_kind.name if purchase_kind else "Закуп материала"

        # Приход накладной — ОДНА строка ленты на документ, а не на позицию:
        # заказчик платит за поставку целиком, и сверяет он тоже её.
        supplies = Supply.objects.select_related("supplier").prefetch_related("lines")
        if d_from:
            supplies = supplies.filter(received_on__gte=d_from)
        if d_to:
            supplies = supplies.filter(received_on__lte=d_to)
        for supply in supplies:
            total = supply.total_cost
            if not total:
                continue
            label = supply.number or f"#{supply.id}"
            rows.append({
                "key": f"supply-{supply.id}",
                "source": "SUPPLY",
                "id": supply.id,
                "kind": purchase_kind.id if purchase_kind else None,
                "kind_name": purchase_name,
                "name": f"Накладная {label}",
                "amount": total,
                "spent_at": supply.received_on,
                "note": supply.supplier.name if supply.supplier_id else "",
            })

        # Одиночные приходы — те, что вводят кнопкой на строке материала, мимо
        # накладной. В отчёте они считаются так же, значит и в ленте должны быть.
        logs = InventoryLog.objects.filter(
            type=InventoryLog.Type.SUPPLY,
            quantity_changed__gt=0,
            actual_price__isnull=False,
            supply__isnull=True,
        ).select_related("material")
        if d_from:
            logs = logs.filter(happened_at__date__gte=d_from)
        if d_to:
            logs = logs.filter(happened_at__date__lte=d_to)
        for log in logs:
            rows.append({
                "key": f"log-{log.id}",
                "source": "SUPPLY",
                "id": log.id,
                "kind": purchase_kind.id if purchase_kind else None,
                "kind_name": purchase_name,
                "name": log.material.name,
                "amount": log.quantity_changed * log.actual_price,
                "spent_at": timezone.localtime(log.happened_at).date(),
                "note": log.reason,
            })

        rows.sort(key=lambda r: (r["spent_at"], r["key"]), reverse=True)
        return Response({
            "results": rows,
            "total": sum((r["amount"] for r in rows), Decimal("0")),
        })


class CompanyProfileView(APIView):
    """GET/PATCH реквизитов организации — шапки печатных документов.

    Читать может ЛЮБОЙ сотрудник: накладную и товарный чек печатает складовщик,
    а без реквизитов у документа не будет шапки. Править — только админ.
    Отдельно от `/finance/settings/` именно из-за прав: там деньги, и складовщику
    туда нельзя.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(CompanyProfileSerializer(CompanyProfile.load()).data)

    def patch(self, request):
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Реквизиты меняет только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = CompanyProfileSerializer(
            CompanyProfile.load(), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        AuditLog.record(request.user, "Изменены реквизиты организации")
        return Response(serializer.data)


class FinanceSettingsView(APIView):
    """GET/PATCH the singleton manual P&L inputs."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        return Response(FinanceSettingsSerializer(FinanceSettings.load()).data)

    def patch(self, request):
        serializer = FinanceSettingsSerializer(
            FinanceSettings.load(), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class FinanceReportView(APIView):
    """GET /api/finance/report/ — P&L like the client's Excel: materials / fixed /
    variable costs with totals, plus revenue, outstanding client debt and profit.

    Manual inputs come from FinanceSettings; «остаток на конец» = live stock value;
    variable costs = sum of Expense rows by category."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        s = FinanceSettings.load()

        # Необязательный период (date_from/date_to — те же имена, что в
        # /audit/dashboard/) двигает ВСЕ денежные показатели: выручку, покупки,
        # вложения, резку, долг, прибыль. Границы включительные.
        d_from = _parse_date(request.query_params.get("date_from"))
        d_to = _parse_date(request.query_params.get("date_to"))

        def by_created(qs, field="created_at"):
            # field — чтобы фильтровать и позиции чеков (там дата у самого чека).
            if d_from:
                qs = qs.filter(**{f"{field}__date__gte": d_from})
            if d_to:
                qs = qs.filter(**{f"{field}__date__lte": d_to})
            return qs

        def by_spent(qs):
            if d_from:
                qs = qs.filter(spent_at__gte=d_from)
            if d_to:
                qs = qs.filter(spent_at__lte=d_to)
            return qs

        # Структура отчёта повторяет Excel заказчика: три блока с подытогами —
        # Материалы, Постоянные расходы, Переменные расходы. Строки блоков — не
        # зашитые категории, а справочник ExpenseKind: админ заводит свои виды
        # («Реклама», «Налоги»), и они сами появляются в нужном блоке.
        spent_by_kind = defaultdict(lambda: Decimal("0"))
        for row in (
            by_spent(ExpenseEntry.objects.all())
            .values("kind_id")
            .annotate(v=_SUM("amount"))
        ):
            spent_by_kind[row["kind_id"]] = row["v"]

        kinds = list(ExpenseKind.objects.filter(is_archived=False))

        # Что система считает сама, чтобы это не пришлось вбивать руками.
        # Закуп материала складывается из приходов на склад: каждое поступление
        # уже несёт свою себестоимость. Ручные записи по этому же виду к нему
        # ДОБАВЛЯЮТСЯ — для покупок, прошедших мимо склада.
        auto_by_code = {
            ExpenseKind.MATERIAL_PURCHASE: purchases_from_stock(d_from, d_to),
        }

        def block_rows(block):
            """Строки блока со суммой за период, в порядке отображения."""
            rows = []
            for k in kinds:
                if k.block != block:
                    continue
                auto = auto_by_code.get(k.code, Decimal("0"))
                manual = spent_by_kind.get(k.id, Decimal("0"))
                rows.append({
                    "id": k.id,
                    "code": k.code,
                    "name": k.name,
                    # Снятый флаг — расход виден, но прибыль не уменьшает
                    # (оборудование, улучшение цеха и любые свои такие виды).
                    "in_profit": k.in_profit,
                    "is_builtin": k.is_builtin,
                    "amount": auto + manual,
                    # Часть, посчитанная системой: интерфейс помечает её и
                    # объясняет, откуда взялась цифра.
                    "auto_amount": auto,
                    "manual_amount": manual,
                })
            return rows

        def block_total(rows):
            return sum(
                (r["amount"] for r in rows if r["in_profit"]), Decimal("0")
            )

        fixed_rows = block_rows(ExpenseKind.Block.FIXED)
        variable_rows = block_rows(ExpenseKind.Block.VARIABLE)
        material_rows = block_rows(ExpenseKind.Block.MATERIALS)

        total_fixed = block_total(fixed_rows)
        operating_variable = block_total(variable_rows)
        # Вложения (оборудование + улучшение цеха и любые другие виды со снятым
        # флагом) показываются В БЛОКЕ переменных расходов, но в прибыль НЕ
        # входят: станок за 300 000 не должен делать месяц убыточным.
        investment_rows = [r for r in variable_rows if not r["in_profit"]]
        investments = {
            "rows": investment_rows,
            "total": sum((r["amount"] for r in investment_rows), Decimal("0")),
        }
        # --- Блок «Материалы» (как в исходном Excel заказчика) ---------------
        # Закуп, транспорт и долг материала — такие же виды расхода с записями,
        # как строки остальных блоков. В расход материала идут строки с флагом
        # «входит в прибыль» (закуп + транспорт + свои виды); долг материала
        # флага не имеет и остаётся справочным — материал, взятый в долг, уже
        # сидит в закупе, иначе посчитали бы дважды.
        #
        #   расход материала = Σ(строки блока) = закуп + транспорт
        #
        # Остатки на начало и на конец из формулы УБРАНЫ (решение заказчика,
        # 2026-08-14). Раньше было «начало + закуп + транспорт − конец»: расход
        # выводился через склад, и пока остатки не заполнены, формула уходила в
        # минус на всю стоимость склада — приходилось держать признак
        # needs_setup и не пускать такой итог в прибыль. Теперь в расход идёт то,
        # что за месяц реально потратили на материал.
        materials_spend = block_total(material_rows)
        materials = {
            "spend": materials_spend,            # сумма строк блока, идущих в итог
            "rows": material_rows,               # виды расхода этого блока
            "total": materials_spend,
        }

        # Себестоимость проданного: закупочная стоимость материала, ушедшего в
        # заказы за период (FIFO по партиям, зафиксирована в момент продажи).
        # В прибыль НЕ входит — её считает блок «Материалы» (решение заказчика);
        # остаётся справочной цифрой для сверки маржи.
        cogs = by_created(
            TransactionItem.objects.filter(is_returned=False).exclude(
                receipt__status=Receipt.Status.CANCELLED
            ),
            field="receipt__created_at",
        ).aggregate(v=_SUM("cost_total"))["v"]

        total_expenses = materials["total"] + total_fixed + operating_variable

        # Выручка = ВСЕ заказы периода, кроме отменённых, за вычетом возвратов.
        # Раньше считались только полностью оплаченные чеки плюс предоплаты по
        # открытым: заказ, отданный в долг, в выручку не попадал вовсе — работа
        # сделана, материал списан, а в отчёте её нет. Долг от этого не исчез,
        # он показан отдельной строкой: сколько из выручки уже на руках
        # (`revenue_paid`), а сколько ещё должны (`client_debt`).
        live = by_created(Receipt.objects.exclude(status=Receipt.Status.CANCELLED))
        revenue = live.aggregate(v=_SUM("total_price"))["v"] - live.aggregate(
            v=_SUM("refunded_amount")
        )["v"]
        # Сколько денег по этим заказам реально приняли — включая предоплаты.
        #
        # По каждому чеку берём НЕ БОЛЬШЕ его вклада в выручку: возврат уменьшает
        # выручку, а `amount_paid` остаётся прежним, и «оплачено» вылезало выше
        # «выручки» — 13 613 против 13 253 при нулевом долге. Плитка сама себе
        # противоречила, и это первое, что заказчик складывает в уме.
        # Лишнее сверх стоимости оставшихся строк — это уже не выручка, а сдача
        # или возвращённые деньги, и они живут в своих полях.
        revenue_paid = Decimal("0")
        for r in live.only("total_price", "amount_paid", "refunded_amount"):
            kept = r.total_price - r.refunded_amount
            revenue_paid += min(r.amount_paid, kept) if kept > 0 else Decimal("0")
        pending = live.filter(payment_status__in=Receipt.OWING_STATUSES)

        # Долг клиентов = Σ (сумма − предоплата − возвраты) по открытым чекам.
        client_debt = Decimal("0")
        for r in pending.only("total_price", "amount_paid", "refunded_amount"):
            owed = r.total_price - r.amount_paid - r.refunded_amount
            if owed > 0:
                client_debt += owed

        # Резка — по СТАНКАМ: ЧПУ и лазер. Раньше строки были по типу материала
        # (Акрил / Форекс / Оргстекло), но заказчик считает работу цеха станками:
        # «сколько наработал ЧПУ, сколько лазер» — это его вопрос, а материал в
        # нём вторичен. Материал никуда не делся: он остался в складском листе,
        # где у каждого материала стоит своя выручка с резки.
        cut_by_machine = defaultdict(lambda: Decimal("0"))
        # Объём работы двумя величинами — одной суммы мало: 12 000 сом это много
        # мелких резов или один большой лист?
        #   ПОГОННЫЕ метры — длина реза, по ней и считается сумма (пог.м × ставка);
        #   КВАДРАТНЫЕ — площадь резаного куска, берётся у материала того же чека.
        # Это РАЗНЫЕ величины, складывать их между собой нельзя.
        area_by_machine = defaultdict(lambda: Decimal("0"))
        pm_by_machine = defaultdict(lambda: Decimal("0"))
        # Сколько квадратных метров прошло через руки каждого сотрудника. Это
        # ответ на «кто сколько отрезал» — вопрос про ЛЮДЕЙ, а не про деньги,
        # поэтому и считается в метрах.
        area_by_user = defaultdict(lambda: Decimal("0"))
        pm_by_user = defaultdict(lambda: Decimal("0"))
        rev_by_user = defaultdict(lambda: Decimal("0"))
        cutting_total = Decimal("0")
        # ОБРЕЗКИ: сколько материала списали, но клиенту не отдали. Полосу 0.5 м
        # от рулона 0.9 отрезают на всю ширину, и 0.4 остаётся в цехе — обычно в
        # мусор. Деньги за полную ширину взяты, и это правильно: материал
        # потрачен весь. Но цифра «сколько я подарил» не считалась НИГДЕ.
        #
        # Это не добавочный расход — он уже сидит в себестоимости проданного.
        # Здесь только та его часть, которая до клиента не дошла.
        offcut_area = Decimal("0")
        offcut_cost = Decimal("0")
        for item in (
            by_created(
                TransactionItem.objects.filter(
                    is_returned=False, used_width__isnull=False
                ).exclude(receipt__status=Receipt.Status.CANCELLED),
                field="receipt__created_at",
            )
            .select_related("material", "roll")
        ):
            offcut_area += item.offcut_area
            offcut_cost += item.offcut_cost
        cutting_area = Decimal("0")
        cutting_pm = Decimal("0")
        cut_receipts = (
            by_created(
                Receipt.objects.filter(
                    items__type=TransactionItem.Type.SERVICE,
                    items__service__kind="CUTTING",
                    items__is_returned=False,
                ).exclude(status=Receipt.Status.CANCELLED)
            )
            .distinct()
            .select_related("cashier")
            .prefetch_related("items__material__type", "items__service", "items__roll")
        )
        for r in cut_receipts:
            items = list(r.items.all())
            # Строки работы мастера. Их количество — это и есть длина реза в
            # погонных метрах, из неё же складывается сумма.
            cut_lines = [
                i
                for i in items
                if i.type == TransactionItem.Type.SERVICE
                and not i.is_returned
                and i.service_id
                and i.service.kind == "CUTTING"
            ]
            # Площадь резаного материала этого чека. Продажа по кв.м даёт её
            # прямо в количестве; продажа листами — через площадь листа; рулон —
            # длина × ширина полотна. Штучный материал (крепёж) площади не имеет
            # и в кв.м не попадает.
            #
            # ВОЗВРАЩЁННЫЕ строки материала СЧИТАЮТСЯ ТОЖЕ. Это метрика работы
            # станка — «сколько прошло через ЧПУ», — а станок отрезал независимо
            # от того, вернул клиент материал потом или нет. Раньше возврат
            # обнулял площадь, а строка работы оставалась живой, и плитка
            # показывала «Лазер 0 кв.м · 444 сом»: денег насчитали, а работы
            # будто не было. Деньги здесь берутся со строки РАБОТЫ и на возврат
            # материала не реагируют — значит и площадь не должна.
            area = Decimal("0")
            for i in items:
                if i.type != TransactionItem.Type.MATERIAL or not i.material_id:
                    continue
                if i.sale_mode == TransactionItem.SaleMode.PIECE:
                    if i.material.piece_area:
                        area += i.quantity * i.material.piece_area
                elif i.sale_mode == TransactionItem.SaleMode.METER:
                    # Ширина ПАРТИИ, с которой резали (у карточки — лишь
                    # значение по умолчанию для приёмки).
                    if i.roll_width:
                        area += i.quantity * i.roll_width
                else:
                    area += i.quantity

            receipt_pm = sum((i.quantity for i in cut_lines), Decimal("0"))
            for idx, line in enumerate(cut_lines):
                machine = line.service.machine or ""
                # ТА ЖЕ сумма, что стоит в чеке: строка округляется вверх до
                # целого сома. Через `quantity × price` отчёт расходился с
                # чеками на копейки, и сверка «по бумаге» переставала сходиться
                # ровно там, где заказчик её и делает.
                rev = line.line_total
                cut_by_machine[machine] += rev
                pm_by_machine[machine] += line.quantity
                cutting_total += rev
                cutting_pm += line.quantity
                # Площадь у чека одна на всех, а станков в нём может быть два.
                # Делим её пропорционально длине реза: у чека с одним станком
                # (обычный случай) он получает всю площадь целиком.
                #
                # Погонные метры вводятся ВРУЧНУЮ и могут быть не введены — тогда
                # делить не по чему. Раньше в этом случае доля выходила нулевой,
                # и площадь попадала в «Резка, всего», но ни в одну строку
                # станка: в итоге «всего 1,56 кв.м», а под ним «ЧПУ 0 кв.м».
                # Делим поровну — резали же на этих станках, сколько бы метров
                # ни забыли вписать.
                if receipt_pm:
                    share = line.quantity / receipt_pm
                else:
                    share = Decimal("1") / Decimal(len(cut_lines))
                area_by_machine[machine] += area * share
                # Сотруднику площадь отдаём целиком и один раз: чек оформил один
                # человек, и дробить её между строками того же чека незачем.
                if idx == 0:
                    area_by_user[r.cashier_id] += area
                pm_by_user[r.cashier_id] += line.quantity
                rev_by_user[r.cashier_id] += rev
            cutting_area += area

        # Строки — станки, по которым в периоде что-то резали. «Без станка» —
        # старые чеки, оформленные до разделения, если у их услуги станок не
        # проставлен: молча прятать их сумму нельзя, иначе строки не сойдутся
        # с «Резка, всего».
        machine_names = dict(PrintingService.Machine.choices)
        # «% ЗП мастера» из настроек цен — доля от стоимости работы резки.
        # Настройка существовала, но нигде не считалась: владелец ставил 4 % и
        # ждал цифру, которой не было. Показываем РАСЧЁТНУЮ долю — справочно,
        # в прибыль она не входит: зарплаты вносятся записями, иначе счёт
        # двойной. Целыми сомами, как и остальные деньги отчёта.
        from services.models import PricingSettings

        master_pct = PricingSettings.load().master_commission_percent or Decimal("0")

        def master_share(amount):
            return (amount * master_pct / Decimal("100")).quantize(Decimal("1"))

        # Сортируем строки по ПЛОЩАДИ, а не по сумме: главная величина блока —
        # квадратные метры, и порядок строк должен объяснять именно её.
        machines = set(cut_by_machine) | set(area_by_machine)
        user_names = dict(
            User.objects.filter(id__in=[u for u in area_by_user if u]).values_list(
                "id", "username"
            )
        )
        cutting = {
            "total": cutting_total,
            "area": q2(cutting_area),
            "running_meters": q2(cutting_pm),
            # Расчётная ЗП мастера от всей работы резки за период — справочно.
            "master_commission_percent": master_pct,
            "master_share": master_share(cutting_total),
            "rows": [
                {
                    "id": machine or None,
                    "name": machine_names.get(machine) or "Без станка",
                    "amount": cut_by_machine.get(machine, Decimal("0")),
                    "area": q2(area_by_machine.get(machine, Decimal("0"))),
                    "running_meters": q2(pm_by_machine.get(machine, Decimal("0"))),
                }
                for machine in sorted(
                    machines, key=lambda m: -area_by_machine.get(m, Decimal("0"))
                )
            ],
            # Кто сколько отрезал. Считается по тому, КТО ОФОРМИЛ ЗАКАЗ —
            # отдельного поля «мастер за станком» в системе нет, и выдавать
            # одно за другое нельзя. В цехе на двух человек это обычно один и
            # тот же человек; если понадобится именно резчик — это отдельное
            # поле в кассе, и вводить его придётся на каждый рез.
            "by_user": [
                {
                    "id": uid,
                    "name": user_names.get(uid) or "Без сотрудника",
                    "area": q2(area),
                    "running_meters": q2(pm_by_user.get(uid, Decimal("0"))),
                    # Стоимость работы реза этого сотрудника и его расчётная доля.
                    "amount": rev_by_user.get(uid, Decimal("0")),
                    "master_share": master_share(rev_by_user.get(uid, Decimal("0"))),
                }
                for uid, area in sorted(area_by_user.items(), key=lambda kv: -kv[1])
            ],
        }

        return Response(
            {
                # Строки блоков — виды расхода из справочника, в порядке
                # отображения. Пояснения «что входит» живут примечанием у
                # каждой записи траты.
                "fixed": {"rows": fixed_rows, "total": total_fixed},
                # Блок «Материалы» — первый в отчёте, как в Excel заказчика.
                "materials": materials,
                # Вложения показываем в этом же блоке, но в total их нет —
                # total идёт в прибыль, вложения нет.
                "variable": {"rows": variable_rows, "total": operating_variable},
                # Себестоимость проданного материала — отдельной строкой, чтобы
                # было видно маржу: выручка − себестоимость = сколько заработали
                # на материале до накладных расходов.
                "cogs": cogs,
                # Обрезки — часть этой же себестоимости, не дошедшая до клиента.
                "offcuts": {
                    "area": offcut_area.quantize(Decimal("0.01")),
                    "cost": offcut_cost.quantize(Decimal("0.01")),
                },
                "gross_margin": revenue - cogs,
                "investments": investments,
                "total_expenses": total_expenses,
                "revenue": revenue,
                # Из чего складывается выручка: сколько уже на руках и сколько
                # ещё должны. Одной суммы мало — «выручка 300 000» при 200 000
                # долга и «выручка 300 000» деньгами это разные месяцы.
                "revenue_paid": revenue_paid,
                "client_debt": client_debt,
                "profit": revenue - total_expenses,
                "cutting": cutting,
                "period": {
                    "from": d_from.isoformat() if d_from else None,
                    "to": d_to.isoformat() if d_to else None,
                },
            }
        )


class DailyReportView(APIView):
    """GET /api/finance/daily/?year=&month= — day-by-day P&L for one calendar
    month, so the admin can see which days were profitable and which weren't
    (a month-end total hides that a single bad day happened).

    Revenue and variable expenses come straight from their dated records
    (Receipt.created_at, Expense.spent_at). Fixed monthly costs (rent/utilities/
    internet/other) are a single ongoing manual figure with no date of their
    own, so they are split evenly across the days of the shown month — a day
    only counts as profitable once its share of rent is covered too."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        today = timezone.localdate()
        try:
            year = int(request.query_params.get("year") or today.year)
            month = int(request.query_params.get("month") or today.month)
        except ValueError:
            return Response({"detail": "Некорректный год/месяц."}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= month <= 12):
            return Response({"detail": "Некорректный месяц."}, status=status.HTTP_400_BAD_REQUEST)

        days_in_month = calendar.monthrange(year, month)[1]
        first_day = date(year, month, 1)
        next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        # Постоянные расходы и зарплаты этого месяца берём записями и, как и
        # раньше, размазываем поровну по дням: аренда платится один раз, но
        # «зарабатывать на неё» нужно каждый день, иначе один день месяца
        # выглядел бы катастрофой, а остальные — незаслуженно прибыльными.
        month_entries = ExpenseEntry.objects.filter(
            spent_at__gte=first_day, spent_at__lt=next_month_first, kind__in_profit=True
        )
        fixed_total = month_entries.filter(
            kind__block=ExpenseKind.Block.FIXED
        ).aggregate(v=_SUM("amount"))["v"]
        fixed_share = fixed_total / days_in_month

        live = Receipt.objects.exclude(status=Receipt.Status.CANCELLED).filter(
            created_at__date__gte=first_day, created_at__date__lt=next_month_first
        )
        # Выручка дня — все заказы этого дня, кроме отменённых, минус возвраты.
        # Та же формула, что в отчёте за месяц: раньше здесь (как и там) в
        # выручку шли только оплаченные чеки и предоплаты, и день, отработанный
        # в долг, выглядел убыточным.
        revenue_by_day = defaultdict(lambda: Decimal("0"))
        by_day = (
            live.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(v=_SUM("total_price"), refunds=_SUM("refunded_amount"))
        )
        for row in by_day:
            revenue_by_day[row["day"]] += row["v"] - row["refunds"]

        variable_by_day = defaultdict(lambda: Decimal("0"))
        # Переменные и транспорт падают на свой день. Вложения (оборудование/
        # улучшение цеха и любые виды со снятым флагом «входит в прибыль») в
        # дневную прибыль не входят — это инвестиции, не операционные расходы.
        expense_rows = (
            month_entries.filter(
                kind__block__in=[ExpenseKind.Block.VARIABLE, ExpenseKind.Block.MATERIALS]
            )
            .values("spent_at")
            .annotate(v=_SUM("amount"))
        )
        for row in expense_rows:
            variable_by_day[row["spent_at"]] += row["v"]

        # ЗАКУП ПО ПРИХОДАМ НА СКЛАД — тот же авторасчёт, что стоит в строке
        # «Закуп материала» месячного отчёта (`purchases_from_stock`), только по
        # дням. Без него график видел лишь записи трат, и пока закуп не вписан
        # руками, на одном экране спорили две «Прибыли»: плитки −14 265,
        # график +3 735. Условия те же, что у плиток: вид расхода есть, не в
        # архиве и с флагом «уменьшает прибыль».
        purchase_kind = ExpenseKind.objects.filter(
            code=ExpenseKind.MATERIAL_PURCHASE, is_archived=False
        ).first()
        if purchase_kind is not None and purchase_kind.in_profit:
            last_day = next_month_first - timedelta(days=1)
            for day, amount in purchases_from_stock_by_day(first_day, last_day).items():
                variable_by_day[day] += amount

        # СЕБЕСТОИМОСТЬ ПРОДАННОГО в дневные расходы НЕ входит.
        #
        # Раньше входила — и на одном экране получались две «Прибыли», которые
        # спорили друг с другом: сверху 6 924, в графике 939. Причина в том, что
        # это были две РАЗНЫЕ формулы под одним словом: плитки считают
        # «выручка − (Материалы + Постоянные + Переменные)», а график добавлял
        # сюда ещё и себестоимость.
        #
        # Правило заказчика: прибыль = выручка − (Материалы + Постоянные +
        # Переменные); себестоимость проданного считается точно (FIFO), но она
        # СПРАВОЧНАЯ и в прибыль не идёт — материал уже посчитан закупом. График
        # обязан жить по тому же правилу, иначе он не «второй взгляд», а второй
        # ответ на тот же вопрос.

        rows = []
        for day_num in range(1, days_in_month + 1):
            d = date(year, month, day_num)
            revenue = revenue_by_day.get(d, Decimal("0"))
            variable = variable_by_day.get(d, Decimal("0"))
            # A day that hasn't happened yet has no profit/loss to show — it
            # would otherwise always render "in the red" for its unearned share
            # of rent before any business was even done that day.
            future = d > today
            rows.append({
                "date": d.isoformat(),
                "day": day_num,
                "revenue": revenue,
                "variable": variable,
                "fixed_share": fixed_share,
                "profit": None if future else revenue - variable - fixed_share,
            })

        # ИТОГ под графиком — за МЕСЯЦ ЦЕЛИКОМ, той же формулой, что плитки
        # сверху: выручка − переменные − постоянные. Суммой дневных прибылей
        # его считать нельзя: у будущих дней прибыли нет (столбик не рисуем,
        # чтобы 20-е число не краснело за неотработанную аренду), и их доля
        # аренды выпадала из итога. 17 августа это давало на одном экране
        # −49 497 в плитке и −35 949 под графиком — расходились ровно на
        # аренду оставшихся 14 дней.
        month_revenue = sum((r["revenue"] for r in rows), Decimal("0"))
        month_variable = sum((r["variable"] for r in rows), Decimal("0"))
        totals = {
            "revenue": month_revenue,
            "variable": month_variable,
            "fixed": fixed_total,
            "profit": month_revenue - month_variable - fixed_total,
        }

        return Response({
            "year": year,
            "month": month,
            "days_in_month": days_in_month,
            "today": today.isoformat() if (today.year == year and today.month == month) else None,
            "rows": rows,
            "totals": totals,
        })


class MaterialReportView(APIView):
    """GET /api/finance/material-report/ — таблица «резка по материалам»: по
    каждому материалу заказов, продано кв.м / листов, сумма материала, сумма
    резки, поступление за период и текущий остаток. Плюс строка ИТОГО.

    Складские колонки повторяют лист заказчика и считаются его формулой:
    ``остаток на начало + поступление − проданные = остаток на конец``, где
    остаток на начало вводится РУКАМИ (MaterialMonthOpening) и привязан к
    календарному месяцу. Материалы с заданной площадью листа считаются в листах.

    Период: ?date_from=&date_to= или ?year=&month=. Колонка `stock` — живой
    остаток системы «на сейчас», он к формуле не относится."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        d_from = _parse_date(request.query_params.get("date_from"))
        d_to = _parse_date(request.query_params.get("date_to"))
        # Месяц можно задать и как year+month — тем же выбором, что в других
        # разделах; разворачиваем его в границы периода.
        try:
            year = int(request.query_params.get("year") or 0)
            month = int(request.query_params.get("month") or 0)
        except (TypeError, ValueError):
            year = month = 0
        if year and 1 <= month <= 12:
            d_from = date(year, month, 1)
            d_to = date(year, month, calendar.monthrange(year, month)[1])
        # Ручной остаток на начало привязан к календарному месяцу, поэтому он
        # есть только когда период — ровно месяц (?year=&month= или диапазон,
        # совпавший с целым месяцем). Для произвольного диапазона начала нет.
        opening_year, opening_month = (year, month) if (year and 1 <= month <= 12) else (None, None)
        if opening_year is None and d_from and d_to:
            last = calendar.monthrange(d_from.year, d_from.month)[1]
            if d_from.day == 1 and d_to == date(d_from.year, d_from.month, last):
                opening_year, opening_month = d_from.year, d_from.month

        def by_receipt_date(qs):
            if d_from:
                qs = qs.filter(receipt__created_at__date__gte=d_from)
            if d_to:
                qs = qs.filter(receipt__created_at__date__lte=d_to)
            return qs

        live = Receipt.objects.exclude(status=Receipt.Status.CANCELLED)
        if d_from:
            live = live.filter(created_at__date__gte=d_from)
        if d_to:
            live = live.filter(created_at__date__lte=d_to)

        # Сумма резки по материалу: работу «Резка» каждого чека относим к
        # материалу этого же чека (как в разбивке по категориям).
        cut_by_mat = defaultdict(lambda: Decimal("0"))
        cut_receipts = (
            live.filter(
                items__type=TransactionItem.Type.SERVICE,
                items__service__kind="CUTTING",
                items__is_returned=False,
            )
            .distinct()
            .prefetch_related("items__material", "items__service")
        )
        for r in cut_receipts:
            items = list(r.items.all())
            cut_rev = sum(
                (
                    i.line_total
                    for i in items
                    if i.type == TransactionItem.Type.SERVICE
                    and not i.is_returned
                    and i.service_id
                    and i.service.kind == "CUTTING"
                ),
                Decimal("0"),
            )
            mat = next(
                (
                    i.material
                    for i in items
                    if i.type == TransactionItem.Type.MATERIAL and not i.is_returned and i.material_id
                ),
                None,
            )
            if mat:
                cut_by_mat[mat.id] += cut_rev

        # Продажи материалов: площадь, листы, метры, сумма материала, число
        # заказов. У рулона единица — погонные метры (`metres`), площадь —
        # справочно, по ширине ПАРТИИ строки; раньше метры METER-строк
        # складывались как кв.м, а рулон с размером листа в карточке считался
        # листами («продано 1 м» → «1.000 кв.м / 0.42 листа»).
        agg = defaultdict(
            lambda: {
                "area": Decimal("0"), "sheets": Decimal("0"), "metres": Decimal("0"),
                "mat_rev": Decimal("0"), "orders": set(),
            }
        )
        mat_items = (
            TransactionItem.objects.filter(
                type=TransactionItem.Type.MATERIAL, is_returned=False, material__isnull=False
            )
            .exclude(receipt__status=Receipt.Status.CANCELLED)
            .select_related("material", "roll")
        )
        mat_items = by_receipt_date(mat_items)
        for it in mat_items:
            m = it.material
            a = agg[m.id]
            q = it.quantity
            if it.sale_mode == TransactionItem.SaleMode.METER:
                a["metres"] += q
                a["area"] += q * it.roll_width
            elif it.sale_mode == TransactionItem.SaleMode.PIECE:
                a["sheets"] += q
                if m.piece_area:
                    a["area"] += q * m.piece_area
            else:
                a["area"] += q
                if m.sells_by_metre:
                    a["metres"] += to_units(m, q, width=it.roll_width)
                elif m.piece_area:
                    a["sheets"] += q / m.piece_area
            a["mat_rev"] += it.line_total   # как в чеке: округление вверх
            a["orders"].add(it.receipt_id)

        # Поступление за период: приход по складским логам (и партии рулонов,
        # и обычный приход пишут SUPPLY с положительным количеством). Заодно
        # собираем приходы по дням — колонки «поступление товар» в таблице
        # заказчика («01.июл — 50, 10.июл — 50»). Рулон — по ПАРТИЯМ и в
        # метрах: у журнала только площадь, а ширина у партий разная.
        received = defaultdict(lambda: Decimal("0"))
        received_days = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
        received_metres = defaultdict(lambda: Decimal("0"))
        received_metres_days = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
        supply = InventoryLog.objects.filter(
            type=InventoryLog.Type.SUPPLY, quantity_changed__gt=0
        )
        if d_from:
            supply = supply.filter(happened_at__date__gte=d_from)
        if d_to:
            supply = supply.filter(happened_at__date__lte=d_to)
        for log in supply.annotate(day=TruncDate("happened_at")).values(
            "material_id", "quantity_changed", "day"
        ):
            received[log["material_id"]] += log["quantity_changed"]
            received_days[log["material_id"]][log["day"]] += log["quantity_changed"]
        rolls_in = Roll.objects.filter(material__is_roll_material=True, width__isnull=False)
        if d_from:
            rolls_in = rolls_in.filter(received_at__date__gte=d_from)
        if d_to:
            rolls_in = rolls_in.filter(received_at__date__lte=d_to)
        for roll in rolls_in.values("material_id", "initial_area", "width", "received_at"):
            metres = roll["initial_area"] / roll["width"]
            received_metres[roll["material_id"]] += metres
            day = timezone.localtime(roll["received_at"]).date()
            received_metres_days[roll["material_id"]][day] += metres

        # Остаток на начало месяца система переносит с конца прошлого месяца
        # сама — вписать его нужно один раз, в месяце начала учёта. Вписанное
        # вручную значение всегда побеждает расчётное (см. material_sheet).
        # Скрытые материалы («Удалить» по материалу, у которого были продажи)
        # берём, но ниже оставим только те их строки, где в периоде реально
        # что-то было. Совсем выкидывать нельзя: продажи прошлых месяцев — это
        # настоящие деньги, и отчёт за тот месяц обязан их показать.
        materials = list(Material.objects.all().order_by("name"))
        sheet_manual = collect_manual(materials)
        sheet_received, sheet_sold = collect_flows(materials)
        target_month = (opening_year, opening_month) if opening_year else None

        rows = []
        for m in materials:
            a = agg.get(m.id)
            # Материал считаем в листах, если у него задана площадь листа, —
            # заказчик ведёт склад именно листами; рулон — в погонных метрах;
            # иначе в его единице.
            unit_code = sheet_unit(m)
            per_sheet = counting_unit(m)
            in_units = lambda v: (v / per_sheet) if per_sheet else v  # noqa: E731
            by_metre = unit_code == "METER"

            # Формула складского листа заказчика:
            #   остаток на конец = остаток на начало + поступление − проданные.
            if target_month:
                opening, has_opening = opening_for(
                    m.id, target_month,
                    manual=sheet_manual, received=sheet_received, sold=sheet_sold,
                )
            else:
                # Период не совпал с календарным месяцем — переносить неоткуда.
                opening, has_opening = Decimal("0"), False
            opening = q2(opening)
            if by_metre:
                received_in_units = q2(received_metres.get(m.id, Decimal("0")))
                sold_in_units = q2(a["metres"] if a else Decimal("0"))
            else:
                received_in_units = q2(in_units(received.get(m.id, Decimal("0"))))
                sold_in_units = q2(in_units(a["area"] if a else Decimal("0")))
            closing = opening + received_in_units - sold_in_units
            rows.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "type": m.type.name if m.type_id else "",
                    "production": m.production.name if m.production_id else "",
                    "orders": len(a["orders"]) if a else 0,
                    "sold_area": a["area"] if a else Decimal("0"),
                    "sold_sheets": a["sheets"] if a else Decimal("0"),
                    "material_revenue": a["mat_rev"] if a else Decimal("0"),
                    "cut_revenue": cut_by_mat.get(m.id, Decimal("0")),
                    "received": received.get(m.id, Decimal("0")),
                    # Остаток — в единице листа: у рулона метры.
                    "stock": (m.metres_remaining or Decimal("0")) if by_metre else m.quantity,
                    "unit": m.unit,
                    # Колонки складской таблицы заказчика, все в одной единице:
                    # начало + поступление − проданные = конец.
                    "counted_in": unit_code,
                    # Остаток на начало посчитан переносом с прошлого месяца;
                    # флаг говорит, что его вписали руками (тогда он победил
                    # расчёт) — интерфейс это помечает.
                    "opening_is_manual": has_opening,
                    "stock_start": opening,
                    "stock_end": closing,
                    "received_qty": received_in_units,
                    "sold_qty": sold_in_units,
                    "receipts": (
                        [
                            {"date": day.isoformat(), "qty": q2(qty)}
                            for day, qty in sorted(received_metres_days.get(m.id, {}).items())
                        ]
                        if by_metre
                        else [
                            {"date": day.isoformat(), "qty": q2(in_units(qty))}
                            for day, qty in sorted(received_days.get(m.id, {}).items())
                        ]
                    ),
                }
            )

        # Скрытый материал без единого движения в периоде из таблицы убираем:
        # админ его удалил, и пустая строка «на память» ему не нужна. Если же в
        # периоде по нему были продажи или приход — строка остаётся, иначе
        # деньги месяца не сойдутся.
        archived_ids = {m.id for m in materials if m.is_archived}
        if archived_ids:
            def has_numbers(row):
                return any(
                    Decimal(str(row[key] or 0)) != 0
                    for key in ("sold_area", "material_revenue", "cut_revenue", "received")
                )

            rows = [r for r in rows if r["id"] not in archived_ids or has_numbers(r)]

        # ИТОГО. Заказы не складываем по строкам: один чек может содержать
        # несколько материалов и посчитался бы дважды — берём уникальные чеки.
        all_orders = set()
        for a in agg.values():
            all_orders |= a["orders"]
        totals = {
            "orders": len(all_orders),
            "sold_area": sum((r["sold_area"] for r in rows), Decimal("0")),
            "sold_sheets": sum((r["sold_sheets"] for r in rows), Decimal("0")),
            "material_revenue": sum((r["material_revenue"] for r in rows), Decimal("0")),
            "cut_revenue": sum((r["cut_revenue"] for r in rows), Decimal("0")),
            "received": sum((r["received"] for r in rows), Decimal("0")),
            # Складские итоги — в штуках/листах, суммировать разные единицы
            # смысла нет, но заказчику важна общая строка «ИТОГО», как в Excel.
            "stock_start": sum((r["stock_start"] for r in rows), Decimal("0")),
            "stock_end": sum((r["stock_end"] for r in rows), Decimal("0")),
            "received_qty": sum((r["received_qty"] for r in rows), Decimal("0")),
            "sold_qty": sum((r["sold_qty"] for r in rows), Decimal("0")),
        }

        return Response({
            "rows": rows,
            "totals": totals,
            # Месяц, к которому привязан ручной остаток на начало. null —
            # период не совпал с календарным месяцем, вводить некуда.
            "opening_month": (
                {"year": opening_year, "month": opening_month} if opening_year else None
            ),
            "period": {
                "from": d_from.isoformat() if d_from else None,
                "to": d_to.isoformat() if d_to else None,
            },
        })


class CashEntryViewSet(viewsets.ModelViewSet):
    """Кассовая книга: движение денег по кассе и по банку.

    Отвечает на вопрос, которого системе не хватало: «сколько сейчас должно быть
    в ящике». Оплаты, сдачу, возвраты и откаты пишет сама система — руками сюда
    вносят то, чего она знать не может: закуп за наличные, зарплату, инкассацию.

    Права как у остальных денежных экранов: админ ведёт, бухгалтер смотрит.
    """

    serializer_class = CashEntrySerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_fields = ["account", "kind", "article"]
    ordering = ["-happened_on", "-created_at"]

    def get_queryset(self):
        qs = CashEntry.objects.select_related("created_by", "receipt")
        d_from = _parse_date(self.request.query_params.get("date_from"))
        d_to = _parse_date(self.request.query_params.get("date_to"))
        if d_from:
            qs = qs.filter(happened_on__gte=d_from)
        if d_to:
            qs = qs.filter(happened_on__lte=d_to)
        return qs

    def perform_create(self, serializer):
        ensure_open(serializer.validated_data.get("happened_on"), "Записать операцию этой датой")
        entry = serializer.save(created_by=self.request.user, is_auto=False)
        AuditLog.record(
            self.request.user,
            f"Касса: {entry.get_kind_display().lower()} {entry.amount} сом "
            f"({entry.get_article_display()}, {entry.get_account_display().lower()})",
        )

    def _guard_auto(self, instance):
        """Записи системы руками не трогаем: они отражают чеки, и правка здесь
        развела бы кассу с продажами — а объяснить расхождение было бы нечем."""
        if instance.is_auto:
            return Response(
                {"detail": "Эту запись создала система по чеку — править её нельзя. "
                           "Нужно изменить сам чек или оплату."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def update(self, request, *args, **kwargs):
        blocked = self._guard_auto(self.get_object())
        return blocked or super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        blocked = self._guard_auto(instance)
        if blocked:
            return blocked
        ensure_open(instance.happened_on, "Удалить кассовую запись закрытого периода")
        AuditLog.record(
            request.user, f"Касса: удалена запись {instance.amount} сом "
                          f"({instance.get_article_display()})"
        )
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=["get"])
    def balance(self, request):
        """Сколько денег есть сейчас и что двигалось за период.

        Остаток считается по ВСЕЙ истории, обороты — за выбранный период: иначе
        «остаток за июль» означал бы разное для разных людей.
        """
        d_from = _parse_date(request.query_params.get("date_from"))
        d_to = _parse_date(request.query_params.get("date_to"))

        def side(account, kind):
            qs = CashEntry.objects.filter(kind=kind)
            if account:
                qs = qs.filter(account=account)
            if d_from:
                qs = qs.filter(happened_on__gte=d_from)
            if d_to:
                qs = qs.filter(happened_on__lte=d_to)
            return qs.aggregate(v=_SUM("amount"))["v"]

        accounts = []
        for value, label in CashEntry.Account.choices:
            accounts.append({
                "account": value,
                "label": str(label),
                "balance": CashEntry.balance(value, upto=d_to),
                "income": side(value, CashEntry.Kind.IN),
                "outcome": side(value, CashEntry.Kind.OUT),
            })
        return Response({
            "accounts": accounts,
            "total": CashEntry.balance(upto=d_to),
            "income": side(None, CashEntry.Kind.IN),
            "outcome": side(None, CashEntry.Kind.OUT),
        })

    @action(detail=False, methods=["post"])
    def count(self, request):
        """Пересчёт кассы: «в ящике столько-то».

        Как инвентаризация на складе: система пишет разницу отдельной строкой,
        а не переписывает историю. Недостача видна и остаётся в книге —
        затирать её значит терять единственный след того, что деньги пропали.
        """
        try:
            counted = Decimal(str(request.data.get("counted")))
        except (TypeError, ValueError, ArithmeticError):
            return Response(
                {"detail": "Укажите, сколько денег насчитали."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        account = request.data.get("account") or CashEntry.Account.CASH
        if account not in CashEntry.Account.values:
            return Response({"detail": "Неизвестный счёт."}, status=status.HTTP_400_BAD_REQUEST)

        current = CashEntry.balance(account)
        diff = counted - current
        if diff == 0:
            return Response({"diff": "0", "detail": "Сошлось — расхождения нет."})
        entry = CashEntry.objects.create(
            account=account,
            kind=CashEntry.Kind.IN if diff > 0 else CashEntry.Kind.OUT,
            article=CashEntry.Article.COUNT,
            amount=abs(diff),
            note=request.data.get("note") or f"Пересчёт: было {current}, насчитали {counted}",
            created_by=request.user,
            is_auto=False,
        )
        AuditLog.record(
            request.user,
            f"Пересчёт кассы ({entry.get_account_display()}): {current} → {counted} сом",
        )
        return Response(
            {"diff": str(diff), "entry": CashEntrySerializer(entry).data},
            status=status.HTTP_201_CREATED,
        )


class PeriodLockView(APIView):
    """GET/PATCH закрытия периода: «по такое-то число трогать нельзя».

    Читают все, кому открыты деньги (бухгалтеру важно знать, что месяц закрыт),
    меняет только админ. Открытие обратно — такое же осознанное действие, как
    закрытие, и оба попадают в журнал: если цифры прошлого месяца всё-таки
    поехали, по журналу видно, кто снял замок.
    """

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        return Response(PeriodLockSerializer(PeriodLock.load()).data)

    def patch(self, request):
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Закрывать и открывать период может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        lock = PeriodLock.load()
        was = lock.closed_through
        serializer = PeriodLockSerializer(lock, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        now = serializer.instance.closed_through
        if now == was:
            return Response(serializer.data)
        AuditLog.record(
            request.user,
            f"Период закрыт по {now:%d.%m.%Y}" if now
            else f"Период ОТКРЫТ (был закрыт по {was:%d.%m.%Y})",
        )
        return Response(serializer.data)
