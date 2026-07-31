import calendar
import secrets
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin
from sales.models import Receipt, TransactionItem
from warehouse.models import Material

from .models import Expense, FinanceSettings, FixedExpense, SalaryPayment
from .serializers import (
    ExpenseSerializer,
    FinanceSettingsSerializer,
    FixedExpenseSerializer,
    SalaryPaymentSerializer,
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


def _material_category(material):
    """Map a material to a cutting-report category by its name."""
    n = (getattr(material, "name", "") or "").lower()
    if "форекс" in n or "forex" in n:
        return "forex"
    if "алюк" in n or "aluk" in n or "aluc" in n:
        return "alukobond"
    if "акрил" in n or "acryl" in n or "акрел" in n:
        return "acryl"
    return "other"


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


class ExpenseViewSet(viewsets.ModelViewSet):
    """Variable costs / investments (фреза, оборудование, улучшение цеха, прочее).
    Admin-only. Listed on the «Расходники/Инвестиции» page; feeds the report."""

    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    permission_classes = [IsAdmin]
    filterset_fields = ["category"]
    ordering = ["-spent_at", "-created_at"]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class FixedExpenseViewSet(viewsets.ModelViewSet):
    """Постоянные расходы записями (аренда, коммуналка, интернет, прочие).
    Фильтры: ?category=, ?year=&month= (месячный) и ?spent_at=<дата> (по дню)."""

    queryset = FixedExpense.objects.all()
    serializer_class = FixedExpenseSerializer
    permission_classes = [IsAdmin]
    filterset_fields = ["category", "spent_at"]
    ordering = ["-spent_at", "-created_at"]

    def get_queryset(self):
        return _filter_by_month(super().get_queryset(), self.request, "spent_at")

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class SalaryPaymentViewSet(viewsets.ModelViewSet):
    """Зарплаты по сотрудникам: кому, сколько, когда."""

    queryset = SalaryPayment.objects.all()
    serializer_class = SalaryPaymentSerializer
    permission_classes = [IsAdmin]
    filterset_fields = ["employee", "paid_at"]
    search_fields = ["employee"]
    ordering = ["-paid_at", "-created_at"]

    def get_queryset(self):
        return _filter_by_month(super().get_queryset(), self.request, "paid_at")

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

        # Раздел «Материалы» убран (транспорт и так входит в цену закупки —
        # см. поступление на Складе). Расходы = постоянные + переменные (покупки).
        # Постоянные расходы и зарплаты — теперь записи с датами, поэтому просто
        # суммируем попавшие в период (раньше месячную сумму приходилось резать
        # пропорционально — см. _prorate_factor, больше не нужен здесь).
        def fixed_cat(category):
            return by_spent(
                FixedExpense.objects.filter(category=category)
            ).aggregate(v=_SUM("amount"))["v"]

        salary_qs = SalaryPayment.objects.all()
        if d_from:
            salary_qs = salary_qs.filter(paid_at__gte=d_from)
        if d_to:
            salary_qs = salary_qs.filter(paid_at__lte=d_to)

        fx = {
            "rent": fixed_cat(FixedExpense.Category.RENT),
            "utilities": fixed_cat(FixedExpense.Category.UTILITIES),
            "internet": fixed_cat(FixedExpense.Category.INTERNET),
            "salary": salary_qs.aggregate(v=_SUM("amount"))["v"],
            "other": fixed_cat(FixedExpense.Category.OTHER),
        }
        total_fixed = fx["rent"] + fx["utilities"] + fx["internet"] + fx["salary"] + fx["other"]

        def cat(category):
            return by_spent(
                Expense.objects.filter(category=category)
            ).aggregate(v=_SUM("amount"))["v"]

        var = {
            "cutter": cat(Expense.Category.CUTTER),
            "equipment": cat(Expense.Category.EQUIPMENT),
            "improvement": cat(Expense.Category.IMPROVEMENT),
            "other": cat(Expense.Category.OTHER),
        }
        # Вложения (оборудование + улучшение цеха) — это ИНВЕСТИЦИИ, а не текущие
        # расходы: в расчёт прибыли не входят, показываются отдельным блоком
        # (решение заказчика). Операционные переменные = расходники (фреза) + прочие.
        investments = {
            "equipment": var["equipment"],
            "improvement": var["improvement"],
            "total": var["equipment"] + var["improvement"],
        }
        operating_variable = var["cutter"] + var["other"]

        # Себестоимость проданного: закупочная стоимость материала, ушедшего в
        # заказы за период. Зафиксирована на строках чека в момент списания
        # (FIFO по партиям), поэтому переоценка склада задним числом её не
        # двигает. Возвращённые строки и отменённые чеки не считаем — товар
        # вернулся на склад.
        cogs = by_created(
            TransactionItem.objects.filter(is_returned=False).exclude(
                receipt__status=Receipt.Status.CANCELLED
            ),
            field="receipt__created_at",
        ).aggregate(v=_SUM("cost_total"))["v"]

        total_expenses = total_fixed + operating_variable + cogs

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
        cutting = {"total": Decimal("0"), "forex": Decimal("0"), "alukobond": Decimal("0"), "acryl": Decimal("0"), "other": Decimal("0")}
        cut_receipts = (
            by_created(
                Receipt.objects.filter(
                    items__type=TransactionItem.Type.SERVICE,
                    items__service__kind="CUTTING",
                    items__is_returned=False,
                ).exclude(status=Receipt.Status.CANCELLED)
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
            cutting["total"] += cut_rev
            cutting[_material_category(mat) if mat else "other"] += cut_rev

        return Response(
            {
                # Пояснения «что входит» больше не отдельные поля настроек: они
                # живут примечанием у каждой записи постоянного расхода.
                "fixed": {
                    "rent": fx["rent"],
                    "utilities": fx["utilities"],
                    "internet": fx["internet"],
                    "salary": fx["salary"],
                    "other": fx["other"],
                    "total": total_fixed,
                },
                "variable": {
                    "cutter": var["cutter"],
                    "other": var["other"],
                    "total": operating_variable,
                },
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
        fixed_total = (
            FixedExpense.objects.filter(
                spent_at__gte=first_day, spent_at__lt=next_month_first
            ).aggregate(v=_SUM("amount"))["v"]
            + SalaryPayment.objects.filter(
                paid_at__gte=first_day, paid_at__lt=next_month_first
            ).aggregate(v=_SUM("amount"))["v"]
        )
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
        expense_rows = (
            Expense.objects.filter(spent_at__gte=first_day, spent_at__lt=next_month_first)
            # Вложения (оборудование/улучшение цеха) в дневную прибыль не входят —
            # это инвестиции, не операционные расходы (как в общем отчёте).
            .exclude(category__in=[Expense.Category.EQUIPMENT, Expense.Category.IMPROVEMENT])
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
    """GET /api/finance/material-report/ — таблица «резка по материалам» как в
    эталоне: по каждому материалу — заказов, продано кв.м / листов, сумма
    материала, сумма резки, текущий остаток. Считается из позиций чеков."""

    permission_classes = [IsAdmin]

    def get(self, request):
        live = Receipt.objects.exclude(status=Receipt.Status.CANCELLED)

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

        rows = []
        for m in Material.objects.all().order_by("name"):
            a = agg.get(m.id)
            rows.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "category": _material_category(m),
                    "orders": len(a["orders"]) if a else 0,
                    "sold_area": a["area"] if a else Decimal("0"),
                    "sold_sheets": a["sheets"] if a else Decimal("0"),
                    "material_revenue": a["mat_rev"] if a else Decimal("0"),
                    "cut_revenue": cut_by_mat.get(m.id, Decimal("0")),
                    "stock": m.quantity,
                    "unit": m.unit,
                }
            )

        return Response({"rows": rows})
