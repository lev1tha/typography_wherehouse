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

from .models import Expense, FinanceSettings
from .serializers import ExpenseSerializer, FinanceSettingsSerializer

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

        # Раздел «Материалы» убран (транспорт и так входит в цену закупки —
        # см. поступление на Складе). Расходы = постоянные + переменные (покупки).
        # Зарплаты — ручное поле (постоянные), FinanceSettings.salary.
        total_fixed = s.rent + s.utilities + s.internet + s.salary + s.fixed_other

        def cat(category):
            return Expense.objects.filter(category=category).aggregate(v=_SUM("amount"))["v"]

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
        total_expenses = total_fixed + operating_variable

        # Выручка = оплаченные чеки (полная сумма) + предоплаты по открытым заказам.
        live = Receipt.objects.exclude(status=Receipt.Status.CANCELLED)
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
            Receipt.objects.filter(
                items__type=TransactionItem.Type.SERVICE,
                items__service__kind="CUTTING",
                items__is_returned=False,
            )
            .exclude(status=Receipt.Status.CANCELLED)
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
                "fixed": {
                    "rent": s.rent,
                    "utilities": s.utilities,
                    "utilities_note": s.utilities_note,
                    "internet": s.internet,
                    "salary": s.salary,
                    "other": s.fixed_other,
                    "other_note": s.fixed_other_note,
                    "total": total_fixed,
                },
                "variable": {
                    "cutter": var["cutter"],
                    "other": var["other"],
                    "total": operating_variable,
                },
                "investments": investments,
                "total_expenses": total_expenses,
                "revenue": revenue,
                "client_debt": client_debt,
                "profit": revenue - total_expenses,
                "cutting": cutting,
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

        s = FinanceSettings.load()
        fixed_total = s.rent + s.utilities + s.internet + s.salary + s.fixed_other
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
