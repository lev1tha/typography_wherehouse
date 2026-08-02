from rest_framework import serializers

from .models import ExpenseEntry, ExpenseKind, FinanceSettings


class ExpenseKindSerializer(serializers.ModelSerializer):
    block_display = serializers.CharField(source="get_block_display", read_only=True)
    entries_count = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseKind
        fields = [
            "id",
            "code",
            "name",
            "block",
            "block_display",
            "in_profit",
            "is_builtin",
            "position",
            "is_archived",
            "entries_count",
        ]
        # Код генерируется из названия, «встроенность» задаёт только миграция.
        read_only_fields = ["code", "is_builtin"]

    def get_entries_count(self, obj) -> int:
        # Вьюха аннотирует список, чтобы не делать запрос на каждую строку.
        annotated = getattr(obj, "entries_total", None)
        return annotated if annotated is not None else obj.entries.count()

    def validate(self, attrs):
        instance = self.instance
        block = attrs.get("block", instance.block if instance else None)
        if instance and instance.is_builtin:
            # Отчёт опирается на встроенные виды по коду: транспорт живёт в
            # блоке «Материалы», зарплаты — с именами сотрудников. Переезд в
            # другой блок сломал бы формулу, поэтому запрещаем (название и
            # порядок менять можно).
            if block != instance.block:
                raise serializers.ValidationError(
                    {"block": "Блок встроенного вида расхода менять нельзя."}
                )
        elif block not in ExpenseKind.USER_BLOCKS:
            raise serializers.ValidationError(
                {"block": "Свой вид расхода можно завести только в «Постоянных» "
                          "или «Переменных расходах»."}
            )
        return attrs

    def create(self, validated_data):
        validated_data["code"] = ExpenseKind.make_code(validated_data.get("name", ""))
        validated_data["is_builtin"] = False
        return super().create(validated_data)


class ExpenseEntrySerializer(serializers.ModelSerializer):
    kind_name = serializers.CharField(source="kind.name", read_only=True)
    kind_block = serializers.CharField(source="kind.block", read_only=True)

    class Meta:
        model = ExpenseEntry
        fields = [
            "id",
            "kind",
            "kind_name",
            "kind_block",
            "name",
            "amount",
            "spent_at",
            "note",
            "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_kind(self, kind):
        # В скрытый вид новые траты не пишем: его специально убрали из отчёта.
        if kind.is_archived:
            raise serializers.ValidationError("Этот вид расхода скрыт.")
        return kind


class FinanceSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinanceSettings
        # Закуп, транспорт и долг материала переехали в виды расхода с
        # записями — здесь остались только остаток на начало и бонус.
        fields = [
            "stock_start",
            "referral_bonus",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]
