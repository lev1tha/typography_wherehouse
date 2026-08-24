from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from .models import (
    CashEntry,
    CompanyProfile,
    ExpenseEntry,
    ExpenseKind,
    FinanceSettings,
    PeriodLock,
)


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
        # Блок «Инвестиции» в прибыль не входит по определению — флаг «входит
        # в прибыль» у его видов всегда снят, что бы ни пришло с формы.
        if block == ExpenseKind.Block.INVESTMENT:
            attrs["in_profit"] = False
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


class CompanyProfileSerializer(serializers.ModelSerializer):
    # Подсказка интерфейсу: пускать ли на счёт на оплату. Считается на сервере,
    # чтобы условие «есть банк и счёт» жило в одном месте.
    has_bank = serializers.BooleanField(read_only=True)

    class Meta:
        model = CompanyProfile
        fields = [
            "name", "inn", "address", "phone",
            "bank_name", "bank_account", "bik",
            "director", "accountant", "note",
            "has_bank", "updated_at",
        ]
        read_only_fields = ["updated_at"]


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


class CashEntrySerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    article_display = serializers.CharField(source="get_article_display", read_only=True)
    account_display = serializers.CharField(source="get_account_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)
    order_number = serializers.IntegerField(source="receipt.order_number", read_only=True)

    class Meta:
        model = CashEntry
        fields = [
            "id", "account", "account_display", "kind", "kind_display",
            "article", "article_display", "amount", "happened_on", "note",
            "receipt", "order_number", "supply", "is_auto",
            "created_by", "created_by_name", "created_at",
            "confirm_negative",
        ]
        read_only_fields = ["is_auto", "created_by", "created_at"]

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Сумма должна быть больше нуля.")
        return value

    def validate_article(self, value):
        # Статьи, которые пишет только система: руками их вносить нельзя, иначе
        # касса разойдётся с чеками и объяснить расхождение будет нечем.
        auto_only = {
            CashEntry.Article.SALE,
            CashEntry.Article.CHANGE,
            CashEntry.Article.REFUND,
            CashEntry.Article.UNPAY,
        }
        if value in auto_only:
            raise serializers.ValidationError(
                "Эту статью система пишет сама — по оплатам, сдаче и возвратам."
            )
        return value

    def validate_happened_on(self, value):
        # Деньги будущим числом — всегда опечатка в дате: остаток «на сегодня»
        # после такой записи показывает то, чего в ящике ещё нет.
        if value and value > timezone.localdate():
            raise serializers.ValidationError("Дата операции не может быть в будущем.")
        return value

    # «Да, я знаю, что остатка не хватает» — осознанное подтверждение из
    # интерфейса. Не поле модели: в базе хранить нечего, это ответ на вопрос.
    confirm_negative = serializers.BooleanField(required=False, write_only=True, default=False)

    def validate(self, attrs):
        # Выдать больше, чем в кассе лежит, обычно означает опечатку — лишний
        # ноль или не тот счёт, — и заметить её можно было только при сверке
        # остатка. Но запрещать наглухо нельзя: кассу вносят не по порядку
        # (расходы за неделю сегодня, приходы завтра), и жёсткий запрет запер бы
        # работу. Поэтому спрашиваем: интерфейс показывает остаток и повторяет
        # запрос с подтверждением, если владелец всё равно хочет записать.
        confirmed = attrs.pop("confirm_negative", False)
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if confirmed or kind != CashEntry.Kind.OUT:
            return attrs
        account = attrs.get("account", getattr(self.instance, "account", None))
        amount = attrs.get("amount", getattr(self.instance, "amount", Decimal("0")))
        balance = CashEntry.balance(account)
        if self.instance is not None and self.instance.kind == CashEntry.Kind.OUT:
            balance += self.instance.amount   # правка своей же записи
        if amount > balance:
            raise serializers.ValidationError({
                "confirm_negative": (
                    f"В кассе «{dict(CashEntry.Account.choices)[account]}» сейчас "
                    f"{balance} сом — выдать {amount} нельзя. Если запись всё же "
                    f"верная, подтвердите её."
                ),
                "balance": str(balance),
            })
        return attrs


class PeriodLockSerializer(serializers.ModelSerializer):
    updated_by_name = serializers.CharField(source="updated_by.username", read_only=True)

    class Meta:
        model = PeriodLock
        fields = ["closed_through", "note", "updated_by", "updated_by_name", "updated_at"]
        read_only_fields = ["updated_by", "updated_at"]

    def validate_closed_through(self, value):
        from django.utils import timezone

        if value and value > timezone.localdate():
            raise serializers.ValidationError(
                "Закрывать будущее нельзя — в нём ещё ничего не произошло."
            )
        return value
