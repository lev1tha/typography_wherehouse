from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from .models import (
    DEFAULT_USEFUL_LIFE_MONTHS,
    CashEntry,
    CompanyProfile,
    ExpenseEntry,
    ExpenseKind,
    FinanceSettings,
    PeriodLock,
    TaxRate,
)
from .periods import add_months, month_start, months_between, parse_month


class MonthField(serializers.Field):
    """Месяц: принимает '2026-10' или '2026-10-15', хранит первое число,
    отдаёт '2026-10' (формат `<input type="month">`)."""

    default_error_messages = {"invalid": "Укажите месяц в виде ГГГГ-ММ."}

    def to_internal_value(self, data):
        if data in (None, ""):
            if self.allow_null:
                return None
            self.fail("invalid")
        value = parse_month(data)
        if value is None:
            self.fail("invalid")
        return value

    def to_representation(self, value):
        return value.strftime("%Y-%m") if value else None


class ExpenseKindSerializer(serializers.ModelSerializer):
    block_display = serializers.CharField(source="get_block_display", read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    entries_count = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseKind
        fields = [
            "id",
            "code",
            "name",
            "block",
            "block_display",
            # Роль — источник истины для отчётов (finance.chart). У своих видов
            # выводится из блока, у встроенных задана миграцией.
            "role",
            "role_display",
            # Устарело, только чтение: выводится из роли (старый интерфейс и
            # «Сводка» до перевода на finance/reports ещё смотрят на него).
            "in_profit",
            "is_builtin",
            "position",
            "is_archived",
            "entries_count",
        ]
        # Код генерируется из названия, «встроенность» и роль задаёт система.
        read_only_fields = ["code", "is_builtin", "role", "in_profit"]

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
                {"block": "Свой вид расхода можно завести в «Материалах», «Постоянных», "
                          "«Переменных расходах» или «Инвестициях»."}
            )
        else:
            # Галочки «входит в прибыль» больше нет (аудит Б-10): свой вид —
            # либо расход месяца, либо покупка в «Инвестициях», третьего нет.
            attrs["role"] = ExpenseKind.role_for_block(block)
        return attrs

    def create(self, validated_data):
        validated_data["code"] = ExpenseKind.make_code(validated_data.get("name", ""))
        validated_data["is_builtin"] = False
        return super().create(validated_data)


class ExpenseEntrySerializer(serializers.ModelSerializer):
    kind_name = serializers.CharField(source="kind.name", read_only=True)
    kind_block = serializers.CharField(source="kind.block", read_only=True)
    kind_role = serializers.CharField(source="kind.role", read_only=True)
    # «За какой месяц»: по нему расход ложится в ОПиУ, по `spent_at` — в кассу.
    # Не передан — месяц оплаты.
    period = MonthField(required=False, allow_null=True)
    depreciate_until = MonthField(required=False, allow_null=True)
    is_capitalized = serializers.BooleanField(read_only=True)

    class Meta:
        model = ExpenseEntry
        fields = [
            "id",
            "kind",
            "kind_name",
            "kind_block",
            "name",
            "amount",
            # Чем заплатили: из ящика или со счёта. Кассовая книга разносит
            # трату по этому полю — иначе остаток наличных считал бы и переводы.
            "account",
            "spent_at",
            "period",
            # Капвложение выше порога: срок службы и месяц выбытия. У остальных
            # трат всегда пусто — сервер чистит их сам.
            "useful_life_months",
            "depreciate_until",
            "is_capitalized",
            "kind_role",
            "note",
            "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_kind(self, kind):
        # В скрытый вид новые траты не пишем: его специально убрали из отчёта.
        if kind.is_archived:
            raise serializers.ValidationError("Этот вид расхода скрыт.")
        # Закуп материала система считает сама — по приходам на склад. Ручная
        # трата этого вида ложилась второй раз в «Закуп» (оплату долга
        # поставщику так и вносили), а мимо склада материал в прибыль не
        # попадал никогда. Оплата поставщику — в приходе или накладной
        # («Финансы» → «Долг поставщикам»), покупка расходника мимо склада —
        # переменным расходом.
        if kind.code == ExpenseKind.MATERIAL_PURCHASE and (
            self.instance is None or self.instance.kind_id != kind.id
        ):
            raise serializers.ValidationError(
                "Закуп считается сам по приходам на склад. Оплату поставщику "
                "проведите в «Долге поставщикам», покупку мимо склада — "
                "переменным расходом."
            )
        return kind

    def validate_useful_life_months(self, value):
        if value is not None and value < 1:
            raise serializers.ValidationError("Срок службы — хотя бы один месяц.")
        return value

    @staticmethod
    def life_cap(kind, spent_at):
        """Наибольший срок службы покупки, месяцев. None — без ограничения.

        Улучшение арендованного цеха служит не дольше аренды (D-13): месяцев от
        начала амортизации (следующий месяц после покупки) до месяца окончания
        аренды включительно, но не меньше одного. Аренда не указана — 60.
        """
        if kind.code != ExpenseKind.IMPROVEMENT:
            return None
        lease_until = FinanceSettings.load().lease_until
        if not lease_until:
            return DEFAULT_USEFUL_LIFE_MONTHS
        left = months_between(add_months(spent_at, 1), lease_until)
        return max(1, min(left, DEFAULT_USEFUL_LIFE_MONTHS))

    def validate(self, attrs):
        inst = self.instance
        kind = attrs.get("kind", inst.kind if inst else None)
        spent_at = attrs.get("spent_at", inst.spent_at if inst else timezone.localdate())
        amount = attrs.get("amount", inst.amount if inst else Decimal("0"))

        # «За какой месяц». Явно передан — берём. Новая трата без него — месяц
        # оплаты. При переносе даты оплаты месяц едет следом, только если стоял
        # по умолчанию (= месяцу старой даты): явно выбранный «за август» не
        # должен меняться оттого, что поправили день оплаты.
        if attrs.get("period"):
            attrs["period"] = month_start(attrs["period"])
        elif inst is None or "period" in attrs:
            attrs["period"] = month_start(spent_at)
        elif "spent_at" in attrs and inst.period == month_start(inst.spent_at):
            attrs["period"] = month_start(spent_at)

        if kind is None or kind.role != ExpenseKind.Role.CAPEX:
            attrs["useful_life_months"] = None
            attrs["depreciate_until"] = None
            return attrs

        # Актив или сразу расход — решается по порогу в момент ввода и при
        # смене суммы или вида (D-22). Иначе — как решили тогда: смена порога
        # в настройках прошлое не переписывает.
        reclassify = inst is None or amount != inst.amount or kind != inst.kind
        if reclassify:
            capitalized = amount >= FinanceSettings.load().capitalization_threshold
        else:
            capitalized = inst.is_capitalized
        if not capitalized:
            if attrs.get("depreciate_until"):
                raise serializers.ValidationError({
                    "depreciate_until": "Покупка дешевле порога капвложения — она уже "
                                        "целиком в расходах, амортизировать нечего."
                })
            attrs["useful_life_months"] = None
            attrs["depreciate_until"] = None
            return attrs

        cap = self.life_cap(kind, spent_at)
        default = cap or DEFAULT_USEFUL_LIFE_MONTHS
        life = attrs.get("useful_life_months")
        if life is None:
            life = inst.useful_life_months if (inst and inst.is_capitalized) else default
        if cap and life > cap:
            raise serializers.ValidationError({
                "useful_life_months": f"Улучшение цеха служит не дольше аренды: не больше "
                                      f"{cap} мес. (аренда до — в настройках финансов)."
            })
        attrs["useful_life_months"] = life

        until = attrs.get("depreciate_until", inst.depreciate_until if inst else None)
        if until and until < month_start(spent_at):
            raise serializers.ValidationError({
                "depreciate_until": "Месяц выбытия не может быть раньше месяца покупки."
            })
        return attrs


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
            # Амортизация: порог капвложения и срок аренды цеха. Меняются
            # свободно — у уже внесённых покупок решение и срок записаны в них.
            "capitalization_threshold",
            "lease_until",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def validate_capitalization_threshold(self, value):
        if value is None or value < 0:
            raise serializers.ValidationError("Порог не может быть отрицательным.")
        return value


class TaxRateSerializer(serializers.ModelSerializer):
    """Ставка налога с выручки и месяц начала действия (D-10)."""

    valid_from = MonthField()
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = TaxRate
        fields = ["id", "valid_from", "rate", "note", "created_by", "created_by_name", "created_at"]
        read_only_fields = ["created_by", "created_at"]

    def validate_rate(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("Ставка — от 0 до 100 %.")
        return value

    def validate_valid_from(self, value):
        others = TaxRate.objects.filter(valid_from=value)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError("С этого месяца ставка уже задана — поправьте её.")
        return value


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
            "receipt", "order_number", "supply", "expense", "is_auto",
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
        # Расход цеха и зарплата, внесённые прямо в кассу, уходили из денег, но
        # не попадали в ОПиУ: касса говорила «потратили», прибыль — нет. Их
        # вносят в «Финансах» — там трата попадает и в ОПиУ, и в кассу разом.
        # Уже внесённые записи остаются (в ОДДС — своей строкой).
        by_expense = {CashEntry.Article.EXPENSE, CashEntry.Article.SALARY}
        changing_to = self.instance is None or self.instance.article != value
        if value in by_expense and changing_to:
            raise serializers.ValidationError(
                "Расходы и зарплату вносите в «Финансах» — оттуда они попадут и в ОПиУ, "
                "и в кассу."
            )
        # Оплата поставщику руками в кассе долг не гасила: деньги ушли, а долг
        # за накладную остался, и его можно было оплатить второй раз (аудит,
        # Б-7). Платят при приёмке или из «Долга поставщикам» — там гасится и
        # долг. Уже внесённые ручные записи остаются.
        if value == CashEntry.Article.SUPPLY and changing_to:
            raise serializers.ValidationError(
                "Оплату поставщику проводите при приёмке или в «Финансах» → «Долг "
                "поставщикам» — так уменьшится и сам долг."
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
