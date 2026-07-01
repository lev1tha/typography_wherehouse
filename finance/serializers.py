from rest_framework import serializers

from .models import Expense, FinanceSettings


class ExpenseSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id",
            "category",
            "category_display",
            "name",
            "amount",
            "spent_at",
            "note",
            "created_at",
        ]
        read_only_fields = ["spent_at", "created_at"]


class FinanceSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinanceSettings
        fields = [
            "stock_start",
            "material_purchase",
            "transport",
            "material_debt",
            "rent",
            "utilities",
            "utilities_note",
            "internet",
            "salary",
            "fixed_other",
            "fixed_other_note",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]
