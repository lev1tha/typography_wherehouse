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
from .auditing import fmt
from .periods import month_start, parse_month


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
    # Двигает ли трата этого вида кассу (списание безнадёжного долга — нет).
    moves_cash = serializers.BooleanField(read_only=True)

    class Meta:
        model = ExpenseKind
        fields = [
            "moves_cash",
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
    # Начисление зарплаты по ведомости — запись системы, руками не правится.
    is_payroll = serializers.SerializerMethodField()
    # Платежи по активу в рассрочку: сколько уже заплачено по карточке.
    installments_paid = serializers.SerializerMethodField()
    asset_name = serializers.CharField(source="asset.name", read_only=True, default=None)
    # «Да, это не дубль»: подтверждение того, что такая же трата (вид, сумма,
    # дата) уже есть, но эта — другая. Не поле модели.
    confirm_duplicate = serializers.BooleanField(required=False, write_only=True, default=False)

    class Meta:
        model = ExpenseEntry
        fields = [
            "id",
            "kind",
            "kind_name",
            "kind_block",
            "name",
            "amount",
            "is_cashless",
            "asset",
            "asset_name",
            "recurring",
            "is_payroll",
            "installments_paid",
            "confirm_duplicate",
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
        read_only_fields = ["created_at", "recurring"]

    def get_is_payroll(self, obj) -> bool:
        return hasattr(obj, "payroll_accrual")

    def get_installments_paid(self, obj):
        if not obj.is_cashless:
            return None
        annotated = getattr(obj, "installments_total", None)
        if annotated is not None:
            return annotated
        return sum((i.amount for i in obj.installments.all()), Decimal("0"))

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

    def validate_amount(self, value):
        # Трата −5 000 вычитала бы из расходов и из кассы, 0 — пустая запись.
        # Ошибочную трату удаляют, а не гасят минусом. Проверка срабатывает
        # только когда сумму передали: старая запись с минусом (если такая
        # есть) правится по остальным полям как раньше.
        if value <= 0:
            raise serializers.ValidationError("Сумма должна быть больше нуля.")
        return value

    def validate_useful_life_months(self, value):
        if value is not None and value < 1:
            raise serializers.ValidationError("Срок службы — хотя бы один месяц.")
        return value

    def validate(self, attrs):
        inst = self.instance
        kind = attrs.get("kind", inst.kind if inst else None)
        spent_at = attrs.get("spent_at", inst.spent_at if inst else timezone.localdate())
        amount = attrs.get("amount", inst.amount if inst else Decimal("0"))

        # Актив в рассрочку (PNL-03): карточка — полная цена без денег;
        # платежи по ней — деньги без расхода в ОПиУ.
        cashless = attrs.get("is_cashless", inst.is_cashless if inst else False)
        if inst is not None and "is_cashless" in attrs and attrs["is_cashless"] != inst.is_cashless:
            raise serializers.ValidationError({"is_cashless": "Признак «без денег» после создания не меняется."})
        asset = attrs.get("asset", inst.asset if inst else None)
        confirmed = attrs.pop("confirm_duplicate", False)
        if inst is None and not confirmed and not cashless and asset is None and kind is not None:
            # Дубль (F11/G3-N2): та же трата дважды — «20 листов при 10
            # физических». Не запрет — просьба подтвердить: бывают две одинаковые
            # оплаты за день.
            twin = ExpenseEntry.objects.filter(
                kind=kind, amount=amount, spent_at=spent_at, is_cashless=False, asset__isnull=True,
            ).first()
            if twin is not None:
                raise serializers.ValidationError({
                    "confirm_duplicate": (
                        f"Такая трата уже есть: «{kind.name}» {fmt(amount)} сом от {spent_at:%d.%m.%Y}"
                        f"{' — ' + twin.name if twin.name else ''}. Если это вторая такая же — "
                        f"подтвердите."
                    ),
                    "duplicate_id": twin.pk,
                })
        if cashless and asset is not None:
            raise serializers.ValidationError({"asset": "Карточка актива сама не может быть платежом по активу."})
        if cashless and (kind is None or kind.role != ExpenseKind.Role.CAPEX):
            raise serializers.ValidationError({
                "is_cashless": "Без денег записывается только карточка актива (вид из «Инвестиций»)."
            })
        if inst is not None and inst.is_cashless and hasattr(inst, "payroll_accrual"):
            raise serializers.ValidationError("Это начисление зарплаты — оно правится в ведомости.")

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

        if asset is not None:
            return self._validate_installment(attrs, inst, kind, asset, amount, spent_at)

        if kind is None or kind.role != ExpenseKind.Role.CAPEX:
            attrs["useful_life_months"] = None
            attrs["depreciate_until"] = None
            return attrs

        # Актив или сразу расход — решается по порогу в момент ввода и при
        # смене суммы или вида (D-22). Иначе — как решили тогда: смена порога
        # в настройках прошлое не переписывает. Карточка актива в рассрочку —
        # всегда актив: владелец сказал «это станок», порог не спрашиваем.
        reclassify = inst is None or amount != inst.amount or kind != inst.kind
        if cashless:
            capitalized = True
        elif reclassify:
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

        cap = ExpenseEntry.life_cap(kind, spent_at)
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


    def _validate_installment(self, attrs, inst, kind, asset, amount, spent_at):
        """Платёж по активу: вид и срок — от карточки, сумма — в пределах цены."""
        if not asset.is_asset_card:
            raise serializers.ValidationError({"asset": "Платёж можно привязать только к карточке актива в рассрочку."})
        if kind is not None and kind.id != asset.kind_id:
            raise serializers.ValidationError({"kind": "Вид платежа должен совпадать с видом актива."})
        attrs["kind"] = asset.kind
        attrs["useful_life_months"] = None
        attrs["depreciate_until"] = None
        paid = sum(
            (i.amount for i in asset.installments.exclude(pk=inst.pk if inst else None)), Decimal("0")
        )
        if paid + amount > asset.amount:
            raise serializers.ValidationError({
                "amount": f"Платежи по активу не могут превышать его цену: уже оплачено {paid}, "
                          f"цена {asset.amount}, остаток {asset.amount - paid}."
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
            # Ведомость: за какой месяц по умолчанию платят расчёт; доля мастера
            # в марже строки (только отчёты маржи, ОПиУ не меняется).
            "payroll_prev_month_until_day",
            "master_share_in_margin",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def validate_payroll_prev_month_until_day(self, value):
        if value is None or value < 0 or value > 31:
            raise serializers.ValidationError("Число месяца — от 0 до 31 (0 — всегда за текущий).")
        return value

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
        fields = [
            "id", "valid_from", "rate", "basis", "note", "created_by", "created_by_name", "created_at",
        ]
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
    client_name = serializers.SerializerMethodField()
    # Остаток счёта сразу после этой операции (cash-05). Для одной записи вне
    # списка (создание, правка) контекст его не несёт — тогда считается заново.
    balance_after = serializers.SerializerMethodField()

    class Meta:
        model = CashEntry
        fields = [
            "id", "account", "account_display", "kind", "kind_display",
            "article", "article_display", "amount", "happened_on", "note",
            "receipt", "order_number", "client_name", "supply", "expense", "is_auto",
            "created_by", "created_by_name", "created_at",
            "reconciled", "reconciled_at", "balance_after",
            "confirm_negative",
        ]
        read_only_fields = ["is_auto", "created_by", "created_at", "reconciled", "reconciled_at"]

    def get_client_name(self, obj):
        client = obj.receipt.client if obj.receipt_id else None
        return client.display_name if client else None

    def get_balance_after(self, obj):
        balances = self.context.get("balances_after")
        if balances is None:
            from . import cash

            balances = cash.balances_after()
        return balances.get(obj.id)

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
