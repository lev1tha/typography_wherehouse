import calendar
import secrets
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, DecimalField, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, Material, MaterialType

from .material_sheet import (
    collect_flows,
    collect_manual,
    counting_unit,
    opening_for,
    purchases_from_stock,
    q2,
)
from .models import ExpenseEntry, ExpenseKind, FinanceSettings
from .serializers import (
    ExpenseEntrySerializer,
    ExpenseKindSerializer,
    FinanceSettingsSerializer,
)

_SUM = lambda field: Coalesce(Sum(field), Decimal("0"), output_field=DecimalField())


class FinanceUnlockView(APIView):
    """POST /api/finance/unlock/ — verify the separate password that gates the
    Finance & detailed-analytics screens (on top of the admin login). Admin-only;
    the password itself lives in settings (FINANCE_PASSWORD, configured via .env),
    so it never ships in the frontend bundle."""

    permission_classes = [IsAdmin]

    def post(self, request):
        supplied = str(request.data.get("password") or "")
        expected = str(getattr(settings, "FINANCE_PASSWORD", "") or "")
        if expected and secrets.compare_digest(supplied, expected):
            return Response({"ok": True})
        return Response({"detail": "Неверный пароль."}, status=status.HTTP_403_FORBIDDEN)


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


def _stock_start_from_sheet(materials, d_from, d_to):
    """Стоимость склада на начало периода — по складскому листу.

    Σ(остаток на начало месяца по материалу × его закупочная цена). Остаток
    берётся тот же, что показан в листе: перенос с прошлого месяца или
    вписанное вручную значение. Период должен совпадать с календарным месяцем —
    остаток на начало привязан к месяцу; иначе считать неоткуда.
    """
    if not (d_from and d_to) or d_from.day != 1:
        return Decimal("0")
    last = calendar.monthrange(d_from.year, d_from.month)[1]
    if d_to != date(d_from.year, d_from.month, last):
        return Decimal("0")

    manual = collect_manual(materials)
    received, sold = collect_flows(materials)
    target = (d_from.year, d_from.month)
    total = Decimal("0")
    for m in materials:
        qty, _is_manual = opening_for(
            m.id, target, manual=manual, received=received, sold=sold
        )
        # Лист считает в листах, склад хранит цену за единицу (кв.м) — при
        # переводе обратно умножаем на площадь листа.
        per_sheet = counting_unit(m)
        in_stock_units = qty * per_sheet if per_sheet else qty
        total += in_stock_units * (m.purchase_price or Decimal("0"))
    return total


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
    permission_classes = [IsAdmin]
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
    permission_classes = [IsAdmin]
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
        serializer.save(created_by=self.request.user)


class FinanceSettingsView(APIView):
    """GET/PATCH the singleton manual P&L inputs."""

    permission_classes = [IsAdmin]

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

    permission_classes = [IsAdmin]

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
        #   расход материала = остаток на начало + Σ(строки блока) − остаток на конец
        #
        # Остаток на конец берём живой, со склада.
        materials_spend = block_total(material_rows)
        all_materials = list(Material.objects.all())
        stock_end = sum(
            (m.quantity * m.purchase_price for m in all_materials), Decimal("0")
        )
        # Остаток на начало система считает по складскому листу: остаток на
        # начало месяца по каждому материалу × его закупочная цена. Раньше это
        # число вбивали руками; ручное значение по-прежнему возможно и
        # побеждает расчёт (пустое поле = «считай сам»).
        stock_start_auto = _stock_start_from_sheet(all_materials, d_from, d_to)
        stock_start_manual = s.stock_start
        stock_start = stock_start_manual if stock_start_manual is not None else stock_start_auto
        # Расход не может быть отрицательным: пока склад не заполнен, «остаток
        # на конец» больше начала со всем закупом, и формула уходит в минус.
        materials_raw = stock_start + materials_spend - stock_end

        # Если склад ещё не заполнен, формула уходит в минус на всю его
        # стоимость — а отрицательный расход РАЗДУЛ БЫ прибыль. В прибыль такой
        # результат не пускаем и отдаём признак, что вводные не готовы.
        materials = {
            "stock_start": stock_start,
            "stock_start_auto": stock_start_auto,
            "stock_start_is_manual": stock_start_manual is not None,
            "stock_end": stock_end,              # считается по складу
            "spend": materials_spend,            # сумма строк блока, идущих в итог
            "rows": material_rows,               # виды расхода этого блока
            "total": max(materials_raw, Decimal("0")),
            "needs_setup": materials_raw < 0,
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

        # Выручка = оплаченные чеки (полная сумма) + предоплаты по открытым заказам.
        live = by_created(Receipt.objects.exclude(status=Receipt.Status.CANCELLED))
        revenue_paid = live.filter(payment_status=Receipt.PaymentStatus.PAID).aggregate(
            v=_SUM("total_price")
        )["v"]
        pending = live.filter(payment_status=Receipt.PaymentStatus.PENDING)
        revenue_prepay = pending.aggregate(v=_SUM("amount_paid"))["v"]
        revenue = revenue_paid + revenue_prepay

        # Долг клиентов = Σ (сумма − предоплата − возвраты) по открытым чекам.
        client_debt = Decimal("0")
        for r in pending.only("total_price", "amount_paid", "refunded_amount"):
            owed = r.total_price - r.amount_paid - r.refunded_amount
            if owed > 0:
                client_debt += owed

        # Сумма резки по материалам: выручку услуги «Резка» каждого чека относим
        # к категории материала этого чека (Форекс / Алюкобонд / Акрил / Прочее).
        # Разбивка идёт по ТИПУ материала из справочника. Раньше тип угадывался
        # подстрокой в названии и на реальной номенклатуре ошибался: «синий
        # бишкек», «день ночь» и «салатовый» — акрил, а падали в «Прочее».
        cut_by_type = defaultdict(lambda: Decimal("0"))
        cutting_total = Decimal("0")
        cut_receipts = (
            by_created(
                Receipt.objects.filter(
                    items__type=TransactionItem.Type.SERVICE,
                    items__service__kind="CUTTING",
                    items__is_returned=False,
                ).exclude(status=Receipt.Status.CANCELLED)
            )
            .distinct()
            .prefetch_related("items__material__type", "items__service")
        )
        for r in cut_receipts:
            items = list(r.items.all())
            cut_rev = sum(
                (
                    i.quantity * i.price_per_item
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
            cutting_total += cut_rev
            cut_by_type[mat.type_id if mat and mat.type_id else None] += cut_rev

        # Строки — только типы, по которым в периоде что-то резали, плюс
        # «Без типа», если такой материал попался.
        type_names = dict(MaterialType.objects.values_list("id", "name"))
        cutting = {
            "total": cutting_total,
            "rows": [
                {"id": type_id, "name": type_names.get(type_id) or "Без типа", "amount": amount}
                for type_id, amount in sorted(
                    cut_by_type.items(), key=lambda kv: -kv[1]
                )
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
                "gross_margin": revenue - cogs,
                "investments": investments,
                "total_expenses": total_expenses,
                "revenue": revenue,
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

    permission_classes = [IsAdmin]

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
        revenue_by_day = defaultdict(lambda: Decimal("0"))
        paid = (
            live.filter(payment_status=Receipt.PaymentStatus.PAID)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(v=_SUM("total_price"))
        )
        for row in paid:
            revenue_by_day[row["day"]] += row["v"]
        pending = (
            live.filter(payment_status=Receipt.PaymentStatus.PENDING)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(v=_SUM("amount_paid"))
        )
        for row in pending:
            revenue_by_day[row["day"]] += row["v"]

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

        # Себестоимость проданного падает на день заказа — иначе день с крупной
        # продажей выглядел бы сверхприбыльным, а материал под неё «бесплатным».
        cogs_rows = (
            TransactionItem.objects.filter(
                is_returned=False,
                receipt__created_at__date__gte=first_day,
                receipt__created_at__date__lt=next_month_first,
            )
            .exclude(receipt__status=Receipt.Status.CANCELLED)
            .annotate(day=TruncDate("receipt__created_at"))
            .values("day")
            .annotate(v=_SUM("cost_total"))
        )
        for row in cogs_rows:
            variable_by_day[row["day"]] += row["v"]

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

        totals = {
            "revenue": sum((r["revenue"] for r in rows), Decimal("0")),
            "variable": sum((r["variable"] for r in rows), Decimal("0")),
            "fixed": fixed_total,
            "profit": sum((r["profit"] for r in rows if r["profit"] is not None), Decimal("0")),
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

    permission_classes = [IsAdmin]

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
                    i.quantity * i.price_per_item
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

        # Продажи материалов: площадь, листы, сумма материала, число заказов.
        agg = defaultdict(
            lambda: {"area": Decimal("0"), "sheets": Decimal("0"), "mat_rev": Decimal("0"), "orders": set()}
        )
        mat_items = (
            TransactionItem.objects.filter(
                type=TransactionItem.Type.MATERIAL, is_returned=False, material__isnull=False
            )
            .exclude(receipt__status=Receipt.Status.CANCELLED)
            .select_related("material")
        )
        mat_items = by_receipt_date(mat_items)
        for it in mat_items:
            m = it.material
            a = agg[m.id]
            q = it.quantity
            if it.sale_mode == TransactionItem.SaleMode.PIECE:
                a["sheets"] += q
                if m.piece_area:
                    a["area"] += q * m.piece_area
            else:
                a["area"] += q
                if m.piece_area:
                    a["sheets"] += q / m.piece_area
            a["mat_rev"] += q * it.price_per_item
            a["orders"].add(it.receipt_id)

        # Поступление за период: приход по складским логам (и партии рулонов,
        # и обычный приход пишут SUPPLY с положительным количеством). Заодно
        # собираем приходы по дням — колонки «поступление товар» в таблице
        # заказчика («01.июл — 50, 10.июл — 50»).
        received = defaultdict(lambda: Decimal("0"))
        received_days = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
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

        # Остаток на начало месяца система переносит с конца прошлого месяца
        # сама — вписать его нужно один раз, в месяце начала учёта. Вписанное
        # вручную значение всегда побеждает расчётное (см. material_sheet).
        materials = list(Material.objects.all().order_by("name"))
        sheet_manual = collect_manual(materials)
        sheet_received, sheet_sold = collect_flows(materials)
        target_month = (opening_year, opening_month) if opening_year else None

        rows = []
        for m in materials:
            a = agg.get(m.id)
            # Материал считаем в листах, если у него задана площадь листа, —
            # заказчик ведёт склад именно листами. Иначе в его единице.
            per_sheet = counting_unit(m)
            in_units = lambda v: (v / per_sheet) if per_sheet else v  # noqa: E731

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
                    "stock": m.quantity,
                    "unit": m.unit,
                    # Колонки складской таблицы заказчика, все в одной единице:
                    # начало + поступление − проданные = конец.
                    "counted_in": "SHEET" if per_sheet else m.unit,
                    # Остаток на начало посчитан переносом с прошлого месяца;
                    # флаг говорит, что его вписали руками (тогда он победил
                    # расчёт) — интерфейс это помечает.
                    "opening_is_manual": has_opening,
                    "stock_start": opening,
                    "stock_end": closing,
                    "received_qty": received_in_units,
                    "sold_qty": sold_in_units,
                    "receipts": [
                        {"date": day.isoformat(), "qty": q2(in_units(qty))}
                        for day, qty in sorted(received_days.get(m.id, {}).items())
                    ],
                }
            )

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
