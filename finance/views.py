import calendar
import secrets
from collections import defaultdict
from datetime import date
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

from accounts.permissions import IsAdminOrAccountantRead, SeesMoney
from audit.models import AuditLog
from sales import reporting
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, Material, Roll, Supply

from . import cash
from .material_sheet import (
    collect_flows,
    collect_manual,
    counting_unit,
    opening_for,
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
    TaxRate,
)
from .periods import (
    PeriodClosed, add_months, ensure_month_open, ensure_open, is_closed, month_start,
)
from .serializers import (
    CashEntrySerializer,
    PeriodLockSerializer,
    CompanyProfileSerializer,
    ExpenseEntrySerializer,
    ExpenseKindSerializer,
    FinanceSettingsSerializer,
    TaxRateSerializer,
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


class ExpenseKindViewSet(viewsets.ModelViewSet):
    """Справочник видов расхода — строк финотчёта. Admin-only.

    Свои виды админ заводит сам («Реклама», «Налоги»); встроенные переименовать
    можно, удалить — нет. Скрытые по умолчанию не отдаются, `?archived=1` их
    показывает (та же логика, что у материалов на складе)."""

    serializer_class = ExpenseKindSerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_fields = ["block", "role", "in_profit"]
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

    def perform_update(self, serializer):
        # «Входит в прибыль» и блок меняют прибыль ВСЕХ месяцев, где есть траты
        # этого вида, — в том числе закрытых. Раньше замок это пропускал: трату
        # в закрытый месяц не внести, а снять у аренды галочку можно, и
        # принятая прибыль сентября вырастала на 25 000.
        #
        # С 2026-10-07 то же самое — смена роли (она следует за блоком): трата
        # из расхода становится покупкой с амортизацией и наоборот. Траты вида
        # ложатся в ОПиУ месяцем «за какой месяц», в кассу — днём оплаты;
        # проверяем самый ранний из них.
        kind = serializer.instance
        data = serializer.validated_data
        changes_profit = (
            ("block" in data and data["block"] != kind.block)
            or ("role" in data and data["role"] != kind.role)
        )
        if changes_profit:
            days = [
                d for pair in kind.entries.values_list("spent_at", "period") for d in pair if d
            ]
            ensure_open(
                min(days) if days else None,
                "Менять блок или роль вида, когда по нему есть траты закрытого периода,",
            )
        serializer.save()

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
        # `?basis=accrued` — отбор по «за какой месяц», а не по дате оплаты:
        # строка расхода в «Сводке» считается так же (2026-10-07, D-2), и окно
        # вида обязано показывать те же траты, что дали её сумму.
        field = "period" if self.request.query_params.get("basis") == "accrued" else "spent_at"
        qs = _filter_by_month(qs, self.request, field)
        d_from = _parse_date(self.request.query_params.get("date_from"))
        d_to = _parse_date(self.request.query_params.get("date_to"))
        if field == "period":
            d_from = month_start(d_from) if d_from else None
        if d_from:
            qs = qs.filter(**{f"{field}__gte": d_from})
        if d_to:
            qs = qs.filter(**{f"{field}__lte": d_to})
        return qs

    def perform_create(self, serializer):
        # Трата задним числом в закрытый месяц изменила бы принятый отчёт — и
        # кассу (дата оплаты), и ОПиУ («за какой месяц»).
        data = serializer.validated_data
        ensure_open(data.get("spent_at"), "Записать трату этой датой")
        ensure_month_open(data.get("period"), "Отнести расход к этому месяцу")
        entry = serializer.save(created_by=self.request.user)
        # Деньги ушли — касса обязана это увидеть. Раньше не видела ни одной
        # траты: показывала один приход, и «сколько в ящике» было завышено на
        # всю аренду с зарплатами (на проде 19.09 — на 86 877 сом).
        cash.sync_expense(entry, user=self.request.user)

    # Поля графика амортизации: их правят и у покупки, сделанной в уже
    # закрытом месяце (станок купили в январе, сломался в октябре), — со своим
    # замком по месяцам графика, а не по дню покупки.
    ASSET_FIELDS = {"useful_life_months", "depreciate_until"}

    def perform_update(self, serializer):
        entry = serializer.instance
        data = serializer.validated_data
        changed = {
            name for name, value in data.items() if getattr(entry, name) != value
        }
        if changed - self.ASSET_FIELDS:
            ensure_open(entry.spent_at, "Править трату закрытого периода")
            ensure_open(data.get("spent_at"), "Перенести трату этой датой")
            if "period" in changed:
                ensure_month_open(entry.period, "Переносить расход из закрытого месяца")
                ensure_month_open(data["period"], "Отнести расход к закрытому месяцу")
        if (
            "useful_life_months" in changed
            and entry.is_capitalized
            and is_closed(add_months(entry.spent_at, 1))
        ):
            # Срок меняет долю КАЖДОГО месяца графика с первого (D-21):
            # закрыт хоть один — менять нельзя, останавливают выбытием.
            raise PeriodClosed(
                "Срок службы менять нельзя: часть графика амортизации уже в закрытом "
                "периоде. Чтобы прекратить амортизацию, укажите «амортизировать до»."
            )
        if "depreciate_until" in changed:
            # Меняются месяцы, начиная с более раннего из старого и нового
            # месяца выбытия: там появляется (или пропадает) списание остатка.
            months = [m for m in (entry.depreciate_until, data.get("depreciate_until")) if m]
            if months:
                ensure_month_open(min(months), "Менять месяц выбытия в закрытом периоде")
        entry = serializer.save()
        # Правка суммы/даты/счёта двигает и кассовую запись: иначе в книге
        # осталась бы старая цифра, и остаток разошёлся бы с отчётом.
        cash.sync_expense(entry, user=self.request.user)

    def perform_destroy(self, instance):
        ensure_open(instance.spent_at, "Удалить трату закрытого периода")
        ensure_month_open(instance.period, "Удалить расход закрытого месяца")
        # Кассовую запись уносит каскад по ссылке `CashEntry.expense`.
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


class TaxRateViewSet(viewsets.ModelViewSet):
    """История ставки налога с выручки (D-10).

    Ставка действует с первого числа своего месяца и до следующей записи.
    Добавить, поправить или убрать ставку можно только с открытого месяца:
    иначе изменился бы налог, а с ним и чистая прибыль уже принятых месяцев.
    """

    serializer_class = TaxRateSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None
    queryset = TaxRate.objects.select_related("created_by").order_by("valid_from")

    WHAT = "Менять ставку налога с закрытого месяца"

    def perform_create(self, serializer):
        ensure_month_open(serializer.validated_data["valid_from"], self.WHAT)
        rate = serializer.save(created_by=self.request.user)
        AuditLog.record(self.request.user, f"Ставка налога: {rate.rate} % с {rate.valid_from:%m.%Y}")

    def perform_update(self, serializer):
        ensure_month_open(serializer.instance.valid_from, self.WHAT)
        ensure_month_open(serializer.validated_data.get("valid_from"), self.WHAT)
        rate = serializer.save()
        AuditLog.record(self.request.user, f"Ставка налога изменена: {rate.rate} % с {rate.valid_from:%m.%Y}")

    def perform_destroy(self, instance):
        ensure_month_open(instance.valid_from, self.WHAT)
        AuditLog.record(self.request.user, f"Ставка налога удалена: {instance}")
        instance.delete()


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
    """GET /api/finance/report/?date_from=&date_to= — «Сводка» «Финансов»
    (см. `finance.reports.summary`). Границы включительные; без них — весь
    период. Денежные итоги — из ОПиУ за тот же период."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        from .reports.summary import finance_summary

        return Response(finance_summary(
            _parse_date(request.query_params.get("date_from")),
            _parse_date(request.query_params.get("date_to")),
        ))


def _year_param(request):
    """?year= → год; пусто или мусор — текущий."""
    try:
        year = int(request.query_params.get("year") or timezone.localdate().year)
    except (TypeError, ValueError):
        return None
    return year if 2000 <= year <= 2100 else None


class PnlView(APIView):
    """GET /api/finance/pnl/?year= — ОПиУ по месяцам года
    (`finance.reports.pnl.pnl_year`). Каждый месяц — та же прибыль, что
    «Сводка» за этот месяц: одна функция."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        from .reports.pnl import pnl_year

        year = _year_param(request)
        if year is None:
            return Response({"detail": "Некорректный год."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(pnl_year(year))


class CashFlowView(APIView):
    """GET /api/finance/cash-flow/?year= — ОДДС по месяцам года, прямым
    методом по кассовой книге (`finance.reports.cashflow`)."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        from .reports.cashflow import cash_flow_year

        year = _year_param(request)
        if year is None:
            return Response({"detail": "Некорректный год."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(cash_flow_year(year))


class DailyReportView(APIView):
    """GET /api/finance/daily/?year=&month= — прибыль по дням одного месяца
    (`finance.reports.daily`). Сумма дней — чистая прибыль месяца «Сводки»."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        from .reports.daily import daily_report

        today = timezone.localdate()
        try:
            year = int(request.query_params.get("year") or today.year)
            month = int(request.query_params.get("month") or today.month)
        except ValueError:
            return Response({"detail": "Некорректный год/месяц."}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            return Response({"detail": "Некорректный месяц."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(daily_report(year, month))


class BridgeView(APIView):
    """GET /api/finance/bridge/?year= — сверка «чистая прибыль → чистый
    денежный поток» по месяцам года (`finance.reports.bridge`)."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        from .reports.bridge import bridge_year

        year = _year_param(request)
        if year is None:
            return Response({"detail": "Некорректный год."}, status=status.HTTP_400_BAD_REQUEST)
        return Response(bridge_year(year))


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

        # Заказы периода — в ЛЮБОМ статусе, строки — живые на конец периода:
        # возврат относится к дню, когда его оформили (`sales.reporting`), и
        # поздний возврат месяц заказа не переписывает. Возвраты, оформленные
        # в периоде по заказам ПРОШЛЫХ периодов, вычитаются ниже — так столбцы
        # таблицы сходятся с выручкой и «Резкой, всего» в отчёте.
        period_receipts = Receipt.objects.all()
        if d_from:
            period_receipts = period_receipts.filter(created_at__date__gte=d_from)
        if d_to:
            period_receipts = period_receipts.filter(created_at__date__lte=d_to)
        prior_returns = reporting.prior_returns(d_from, d_to)

        def is_cut(i):
            return (
                i.type == TransactionItem.Type.SERVICE
                and i.service_id
                and i.service.kind == "CUTTING"
            )

        def cut_material(items):
            # Работу относим к материалу того же чека — живому, а если его
            # вернули, то всё равно к нему: резали именно его.
            mats = [i for i in items if i.type == TransactionItem.Type.MATERIAL and i.material_id]
            live_mat = next((i.material for i in mats if not i.is_returned), None)
            return live_mat or (mats[0].material if mats else None)

        # Сумма резки по материалу: работу «Резка» каждого чека относим к
        # материалу этого же чека (как в разбивке по категориям).
        cut_by_mat = defaultdict(lambda: Decimal("0"))
        # Резка материала, принесённого клиентом, — своей строкой (см. ниже).
        own_material_cut = Decimal("0")
        own_material_orders = set()
        cut_receipts = (
            period_receipts.filter(
                items__type=TransactionItem.Type.SERVICE,
                items__service__kind="CUTTING",
            )
            .distinct()
            .prefetch_related("items__material", "items__service")
        )
        for r in cut_receipts:
            items = list(r.items.all())
            live_cuts = [i for i in items if is_cut(i) and reporting.counts_at(i, d_to)]
            if not live_cuts:
                continue
            cut_rev = sum((i.sold_total for i in live_cuts), Decimal("0"))
            mat = cut_material(items)
            # Резка СВОЕГО материала клиента: строки материала в чеке нет, и
            # отнести работу не к чему. Раньше такая сумма просто выпадала из
            # таблицы, и столбец «Резка» не сходился с плиткой «Резка, всего» в
            # отчёте — на проде 19.09 это 83 075 против 84 363. Собираем её в
            # отдельную строку: работа сделана, деньги получены, и в таблице
            # заказчика они должны стоять.
            if mat:
                cut_by_mat[mat.id] += cut_rev
            else:
                own_material_cut += cut_rev
                own_material_orders.add(r.id)
        for line in prior_returns.select_related("service").prefetch_related(
            "receipt__items__material"
        ):
            if not is_cut(line):
                continue
            mat = cut_material(list(line.receipt.items.all()))
            if mat:
                cut_by_mat[mat.id] -= line.sold_total
            else:
                own_material_cut -= line.sold_total

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
        mat_items = by_receipt_date(
            TransactionItem.objects.filter(
                type=TransactionItem.Type.MATERIAL, material__isnull=False
            ).select_related("material", "roll", "receipt")
        )

        def add_line(it, sign):
            m = it.material
            a = agg[m.id]
            q = it.quantity * sign
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
            a["mat_rev"] += it.sold_total * sign   # как в чеке: округление вверх

        for it in mat_items:
            if not reporting.counts_at(it, d_to):
                continue
            add_line(it, 1)
            agg[it.material_id]["orders"].add(it.receipt_id)
        # Возврат материала по заказу прошлого периода — минус в периоде
        # возврата: материал вернулся на полку тогда же.
        for it in prior_returns.filter(
            type=TransactionItem.Type.MATERIAL, material__isnull=False
        ).select_related("material", "roll"):
            add_line(it, -1)

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

        # Резка своего материала клиента — строкой без материала. Ставим её
        # последней: это работа станка, а не движение склада, и складские
        # колонки у неё пустые.
        if own_material_cut:
            rows.append({
                "id": None,
                "name": "Материал клиента",
                "type": "", "production": "",
                "orders": len(own_material_orders),
                "sold_area": Decimal("0"), "sold_sheets": Decimal("0"),
                "material_revenue": Decimal("0"),
                "cut_revenue": own_material_cut,
                "received": Decimal("0"),
                "stock": Decimal("0"),
                "unit": "", "counted_in": "",
                "opening_is_manual": False,
                "stock_start": Decimal("0"), "stock_end": Decimal("0"),
                "received_qty": Decimal("0"), "sold_qty": Decimal("0"),
                "receipts": [],
            })

        # ИТОГО. Заказы не складываем по строкам: один чек может содержать
        # несколько материалов и посчитался бы дважды — берём уникальные чеки.
        all_orders = set()
        for a in agg.values():
            all_orders |= a["orders"]
        # Чеки с резкой своего материала — тоже заказы периода, и в «ИТОГО
        # заказов» они входят: строки материала в них нет, поэтому через `agg`
        # они не пришли бы.
        all_orders |= own_material_orders
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
    в ящике». Оплаты, сдачу, возвраты, откаты и траты из «Финансов» пишет сама
    система — руками сюда вносят то, чего она знать не может: закуп за наличные,
    инкассацию, внесение денег.

    Права как у остальных денежных экранов: админ ведёт, бухгалтер смотрит.
    """

    serializer_class = CashEntrySerializer
    permission_classes = [IsAdminOrAccountantRead]
    filterset_fields = ["account", "kind", "article"]
    ordering = ["-happened_on", "-created_at"]

    def get_queryset(self):
        qs = CashEntry.objects.select_related("created_by", "receipt", "expense__kind")
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
            where = "трате" if instance.expense_id else "чеку"
            what = "саму трату" if instance.expense_id else "сам чек или оплату"
            return Response(
                {"detail": f"Эту запись создала система по {where} — править её нельзя. "
                           f"Нужно изменить {what}."},
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
        # СДАЧА КЛИЕНТОВ, которую ещё не вернули (`Receipt.change_due`). Это
        # чужие деньги: они лежат в кассе, но выручкой не стали и уйдут либо на
        # руки, либо в зачёт следующего заказа. Без этой строки касса спорила с
        # финотчётом: на проде 19.09 в книге 245 453, а «получено по заказам»
        # 245 396 — ровно на 57 сом сдачи по чеку №21, которую не выдали.
        change_held = Receipt.objects.exclude(
            status=Receipt.Status.CANCELLED
        ).aggregate(v=_SUM("change_due"))["v"]
        return Response({
            "accounts": accounts,
            "total": CashEntry.balance(upto=d_to),
            "income": side(None, CashEntry.Kind.IN),
            "outcome": side(None, CashEntry.Kind.OUT),
            "change_held": change_held,
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
