"""API ведомости зарплаты (`finance.payroll`). Админ ведёт, бухгалтер смотрит."""
from __future__ import annotations

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdminOrAccountantRead
from audit.models import AuditLog

from . import auditing, payroll, recurring
from .exports import csv_response
from .models import PayrollAdjustment, PayrollPayment, PayScheme, RecurringExpense
from .payroll_serializers import (
    PayrollAdjustmentSerializer,
    PayrollPaymentSerializer,
    PaySchemeSerializer,
    RecurringExpenseSerializer,
)
from .periods import ensure_month_open, ensure_open, month_start, parse_month
from .views import _parse_date


def _month_param(request, default=None):
    """?month=2026-10 → первое число; пусто — `default` (по умолчанию текущий)."""
    value = request.query_params.get("month") or (request.data.get("month") if hasattr(request, "data") else None)
    month = parse_month(value) if value else None
    return month or default or month_start(timezone.localdate())


class PaySchemeViewSet(viewsets.ModelViewSet):
    """Правила оплаты сотрудников с датой начала (STAFF-05).

    Новое правило действует с первого числа своего месяца; прошлые месяцы
    считаются по прежнему. Добавить, поправить или убрать правило можно только
    с открытого месяца — как ставку налога."""

    serializer_class = PaySchemeSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None
    filterset_fields = ["employee"]
    WHAT = "Менять правила оплаты с закрытого месяца"

    def get_queryset(self):
        return PayScheme.objects.select_related("employee").prefetch_related("rates").order_by(
            "employee_id", "-valid_from"
        )

    @staticmethod
    def _describe(scheme):
        rates = ", ".join(f"{r.get_work_display()} {r.percent} %" for r in scheme.rates.all()) or "без процентов"
        bonus = (
            f", премия {auditing.fmt(scheme.bonus_amount)} при выработке > {auditing.fmt(scheme.bonus_threshold)}"
            if scheme.bonus_threshold is not None and scheme.bonus_amount else ""
        )
        return (
            f"{scheme.employee.full_name} с {scheme.valid_from:%m.%Y}: оклад "
            f"{auditing.fmt(scheme.salary)}, {rates}{bonus}"
        )

    def perform_create(self, serializer):
        ensure_month_open(serializer.validated_data["valid_from"], self.WHAT)
        scheme = serializer.save(created_by=self.request.user)
        auditing.record(self.request.user, f"Правила оплаты добавлены: {self._describe(scheme)}", "payroll")

    def perform_update(self, serializer):
        ensure_month_open(serializer.instance.valid_from, self.WHAT)
        ensure_month_open(serializer.validated_data.get("valid_from"), self.WHAT)
        before = self._describe(serializer.instance)
        scheme = serializer.save()
        auditing.record(
            self.request.user,
            f"Правила оплаты изменены: было — {before}; стало — {self._describe(scheme)}", "payroll",
        )

    def perform_destroy(self, instance):
        ensure_month_open(instance.valid_from, self.WHAT)
        text = self._describe(instance)
        instance.delete()
        auditing.record(self.request.user, f"Правила оплаты удалены: {text}", "payroll")


class PayrollView(APIView):
    """GET /finance/payroll/?month=2026-10 — ведомость за месяц."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        return Response(payroll.statement(_month_param(request)))


class PayrollAccrueView(APIView):
    """POST /finance/payroll/accrue/ {month} — провести начисления в ОПиУ
    (повтор пересчитывает); DELETE — снять проведение месяца."""

    permission_classes = [IsAdminOrAccountantRead]

    def post(self, request):
        month = _month_param(request)
        result = payroll.accrue(month, user=request.user)
        auditing.record(
            request.user, f"Зарплата за {month:%m.%Y} начислена: {result['count']} чел.", "payroll",
        )
        return Response(payroll.statement(month), status=status.HTTP_200_OK)

    def delete(self, request):
        month = _month_param(request)
        n = payroll.unpost(month)
        auditing.record(request.user, f"Начисление зарплаты за {month:%m.%Y} снято: {n} чел.", "payroll")
        return Response(payroll.statement(month))


class PayrollPaymentViewSet(viewsets.ModelViewSet):
    """Аванс и выплата сотруднику: запись выплаты + расход в кассе."""

    serializer_class = PayrollPaymentSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None
    filterset_fields = ["employee", "kind"]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = PayrollPayment.objects.select_related("employee")
        month = parse_month(self.request.query_params.get("month"))
        return qs.filter(period=month) if month else qs

    def create(self, request, *args, **kwargs):
        """Выплата, похожая на ошибку (больше «к выдаче», за будущий месяц,
        отключённому), — сначала вопрос: 409 `needs_confirmation` со списком
        `warnings`; повтор с `confirm_warnings: true` проводит (RF-N3, D-185)."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        ensure_open(data.get("paid_on") or timezone.localdate(), "Записать выплату зарплаты этой датой")
        confirmed = str(request.data.get("confirm_warnings", "")).lower() in ("1", "true", "yes", "on")
        if not confirmed:
            warnings = payroll.payment_warnings(
                data["employee"], kind=data["kind"], amount=data["amount"],
                paid_on=data.get("paid_on"), period=data.get("period"),
            )
            if warnings:
                return Response(
                    {
                        "detail": " ".join(w["message"] for w in warnings) + " Всё верно?",
                        "needs_confirmation": True,
                        "warnings": warnings,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        data = serializer.validated_data
        payment = payroll.pay(
            data["employee"], kind=data["kind"], amount=data["amount"],
            paid_on=data.get("paid_on"), period=data.get("period"),
            account=data.get("account", "CASH"), note=data.get("note", ""), user=self.request.user,
        )
        serializer.instance = payment
        auditing.record(
            self.request.user,
            f"Выплата зарплаты: {payment.employee.full_name} — {payment.get_kind_display().lower()} "
            f"{auditing.fmt(payment.amount)} сом за {payment.period:%m.%Y} "
            f"({auditing.fmt(payment.paid_on)}, {'банк' if payment.account == 'BANK' else 'наличные'})",
            "payroll",
        )

    def perform_destroy(self, instance):
        text = (
            f"Выплата зарплаты удалена: {instance.employee.full_name} — "
            f"{instance.get_kind_display().lower()} {auditing.fmt(instance.amount)} сом "
            f"за {instance.period:%m.%Y} от {auditing.fmt(instance.paid_on)}"
        )
        payroll.remove_payment(instance, user=self.request.user)
        auditing.record(self.request.user, text, "payroll")


class PayrollAdjustmentViewSet(viewsets.ModelViewSet):
    """Удержания (штраф, брак): уменьшают расход на зарплату месяца."""

    serializer_class = PayrollAdjustmentSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None
    filterset_fields = ["employee"]
    WHAT = "Менять удержание закрытого месяца"

    def get_queryset(self):
        qs = PayrollAdjustment.objects.select_related("employee")
        month = parse_month(self.request.query_params.get("month"))
        return qs.filter(month=month) if month else qs

    def _text(self, adj, head):
        return (
            f"{head}: {adj.employee.full_name} — {adj.get_reason_display().lower()} "
            f"{auditing.fmt(adj.amount)} сом за {adj.month:%m.%Y}" + (f" ({adj.note})" if adj.note else "")
        )

    def perform_create(self, serializer):
        ensure_month_open(serializer.validated_data["month"], self.WHAT)
        adj = serializer.save(created_by=self.request.user)
        auditing.record(self.request.user, self._text(adj, "Удержание добавлено"), "payroll")

    def perform_update(self, serializer):
        ensure_month_open(serializer.instance.month, self.WHAT)
        ensure_month_open(serializer.validated_data.get("month"), self.WHAT)
        before = self._text(serializer.instance, "было")
        adj = serializer.save()
        auditing.record(
            self.request.user, f"Удержание изменено: {before}; {self._text(adj, 'стало')}", "payroll",
        )

    def perform_destroy(self, instance):
        ensure_month_open(instance.month, self.WHAT)
        text = self._text(instance, "Удержание удалено")
        instance.delete()
        auditing.record(self.request.user, text, "payroll")


class PayrollDefaultPeriodView(APIView):
    """GET /finance/payroll/default-period/?kind=PAYOUT&paid_on=2026-10-05 —
    за какой месяц по умолчанию платят (настройка `payroll_prev_month_until_day`)."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        kind = request.query_params.get("kind") or PayrollPayment.Kind.PAYOUT
        paid_on = _parse_date(request.query_params.get("paid_on")) or timezone.localdate()
        period = payroll.default_period(kind, paid_on)
        return Response({"period": period.strftime("%Y-%m")})


class PayrollExportView(APIView):
    """GET /finance/payroll/export/?month=2026-10 — ведомость в CSV (Excel откроет)."""

    permission_classes = [IsAdminOrAccountantRead]

    def get(self, request):
        month = _month_param(request)
        return csv_response(
            payroll.statement_csv_rows(payroll.statement(month)), f"vedomost-{month:%Y-%m}.csv"
        )


class RecurringExpenseViewSet(viewsets.ModelViewSet):
    """Повторяющиеся траты: правило, по которому система сама вносит траты.

    Правка правила влияет только на ещё не внесённые месяцы; внесённые траты
    остаются и правятся как обычные."""

    serializer_class = RecurringExpenseSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None

    def get_queryset(self):
        return RecurringExpense.objects.select_related("kind")

    LABELS = {
        "kind_id": "вид", "name": "за что", "amount": "сумма", "account": "счёт", "day": "число",
        "start_month": "с месяца", "until_month": "по месяц", "is_active": "действует",
    }

    @staticmethod
    def _describe(rule):
        until = f" по {rule.until_month:%m.%Y}" if rule.until_month else ""
        return (
            f"«{rule.kind.name}» {rule.name}".strip()
            + f" {auditing.fmt(rule.amount)} сом, каждого {rule.day}-го, с {rule.start_month:%m.%Y}{until}"
        )

    def perform_create(self, serializer):
        rule = serializer.save(created_by=self.request.user)
        auditing.record(self.request.user, f"Повторяющаяся трата добавлена: {self._describe(rule)}", "expense")

    def perform_update(self, serializer):
        before = auditing.snapshot(serializer.instance, self.LABELS)
        rule = serializer.save()
        diff = auditing.changes(before, auditing.snapshot(rule, self.LABELS), self.LABELS)
        auditing.record(
            self.request.user,
            f"Повторяющаяся трата «{rule.kind.name}» {rule.name} изменена: {diff or 'без изменений'}",
            "expense",
        )

    def perform_destroy(self, instance):
        text = self._describe(instance)
        instance.delete()
        auditing.record(self.request.user, f"Повторяющаяся трата удалена: {text}", "expense")


class RecurringRunView(APIView):
    """POST /finance/recurring/run/ — внести недостающие траты по расписанию.

    Безопасно вызывать при каждом открытии «Финансов»: внесённое не задваивается."""

    permission_classes = [IsAdminOrAccountantRead]

    def post(self, request):
        result = recurring.generate(user=request.user)
        return Response({
            "created": len(result["created"]),
            "skipped_closed": sum(1 for s in result["skipped"] if s["reason"] == "closed"),
            # Месяцы правила «Зарплаты», уже начисленные ведомостью (D-162).
            "skipped_payroll": sum(1 for s in result["skipped"] if s["reason"] == "payroll"),
            "details": result["created"],
        })
