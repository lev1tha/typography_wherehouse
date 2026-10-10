from decimal import Decimal

from rest_framework import serializers

from .models import (
    ExpenseKind,
    PayRate,
    PayrollAdjustment,
    PayrollPayment,
    PayScheme,
    RecurringExpense,
)
from .periods import month_start
from .serializers import MonthField


class PayRateSerializer(serializers.ModelSerializer):
    work_display = serializers.CharField(source="get_work_display", read_only=True)

    class Meta:
        model = PayRate
        fields = ["id", "work", "work_display", "percent"]

    def validate_percent(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("Процент — от 0 до 100.")
        return value


class PaySchemeSerializer(serializers.ModelSerializer):
    """Правила оплаты с датой начала; проценты по видам работ — вложенным списком."""

    valid_from = MonthField()
    rates = PayRateSerializer(many=True, required=False)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    class Meta:
        model = PayScheme
        fields = [
            "id", "employee", "employee_name", "valid_from", "salary",
            "bonus_metric", "bonus_threshold", "bonus_amount", "note", "rates", "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_salary(self, value):
        if value < 0:
            raise serializers.ValidationError("Оклад не может быть отрицательным.")
        return value

    def validate_bonus_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Премия не может быть отрицательной.")
        return value

    def validate_bonus_threshold(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Порог не может быть отрицательным.")
        return value

    def validate_rates(self, rows):
        works = [r["work"] for r in rows]
        if len(works) != len(set(works)):
            raise serializers.ValidationError("Вид работы указан дважды.")
        return rows

    def validate(self, attrs):
        employee = attrs.get("employee", self.instance.employee if self.instance else None)
        first = month_start(attrs.get("valid_from", self.instance.valid_from if self.instance else None))
        if employee and first:
            others = PayScheme.objects.filter(employee=employee, valid_from=first)
            if self.instance is not None:
                others = others.exclude(pk=self.instance.pk)
            if others.exists():
                raise serializers.ValidationError(
                    {"valid_from": "С этого месяца правила уже заданы — поправьте их."}
                )
        return attrs

    def _save_rates(self, scheme, rows):
        scheme.rates.all().delete()
        for row in rows:
            PayRate.objects.create(scheme=scheme, work=row["work"], percent=row["percent"])

    def create(self, validated_data):
        rows = validated_data.pop("rates", [])
        scheme = super().create(validated_data)
        self._save_rates(scheme, rows)
        return scheme

    def update(self, instance, validated_data):
        rows = validated_data.pop("rates", None)
        scheme = super().update(instance, validated_data)
        if rows is not None:
            self._save_rates(scheme, rows)
        return scheme


class PayrollAdjustmentSerializer(serializers.ModelSerializer):
    month = MonthField()
    reason_display = serializers.CharField(source="get_reason_display", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    class Meta:
        model = PayrollAdjustment
        fields = [
            "id", "employee", "employee_name", "month", "reason", "reason_display",
            "amount", "note", "inventory_log", "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_amount(self, value):
        if value <= Decimal("0"):
            raise serializers.ValidationError("Сумма удержания должна быть больше нуля.")
        return value


class PayrollPaymentSerializer(serializers.ModelSerializer):
    period = MonthField(required=False, allow_null=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)

    class Meta:
        model = PayrollPayment
        fields = [
            "id", "employee", "employee_name", "period", "kind", "kind_display",
            "amount", "paid_on", "account", "note", "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_amount(self, value):
        if value <= Decimal("0"):
            raise serializers.ValidationError("Сумма должна быть больше нуля.")
        return value

    def validate_account(self, value):
        if value not in ("CASH", "BANK"):
            raise serializers.ValidationError("Счёт — наличные или банк.")
        return value


class RecurringExpenseSerializer(serializers.ModelSerializer):
    start_month = MonthField()
    until_month = MonthField(required=False, allow_null=True)
    kind_name = serializers.CharField(source="kind.name", read_only=True)
    entries_count = serializers.SerializerMethodField()

    class Meta:
        model = RecurringExpense
        fields = [
            "id", "kind", "kind_name", "name", "amount", "account", "day", "start_month",
            "until_month", "is_active", "note", "entries_count", "created_at",
        ]
        read_only_fields = ["created_at"]

    def get_entries_count(self, obj) -> int:
        return obj.entries.count()

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Сумма должна быть больше нуля.")
        return value

    def validate_day(self, value):
        if value < 1 or value > 31:
            raise serializers.ValidationError("Число месяца — от 1 до 31.")
        return value

    def validate_kind(self, kind):
        if kind.is_archived:
            raise serializers.ValidationError("Этот вид расхода скрыт.")
        if kind.role not in (ExpenseKind.Role.OPEX, ExpenseKind.Role.INTEREST, ExpenseKind.Role.TAX):
            raise serializers.ValidationError(
                "По расписанию вносятся обычные расходы, проценты и налог. Закуп, покупки "
                "оборудования и записи без денег расписанием не ведутся."
            )
        return kind

    def validate(self, attrs):
        start = attrs.get("start_month", self.instance.start_month if self.instance else None)
        until = attrs.get("until_month", self.instance.until_month if self.instance else None)
        if start and until and until < month_start(start):
            raise serializers.ValidationError({"until_month": "«По месяц» не может быть раньше «с месяца»."})
        return attrs
