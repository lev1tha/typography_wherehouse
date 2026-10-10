from decimal import ROUND_CEILING, Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import serializers

from .models import Client, ClientSettings


# Продажа, которая состоялась: у неоплаченного онлайн-счёта выручка не признана
# (D-7, D-37 — «не выручка, не долг, нигде не числится»), и в сумму покупок
# клиента он входить не должен.
RECOGNIZED = Q(revenue_recognized_at__isnull=False)


# Телефон: короче девяти цифр это не номер, длиннее пятнадцати (E.164) — тоже.
MIN_PHONE_DIGITS = 9
MAX_PHONE_DIGITS = 15


def client_ltv(client) -> Decimal:
    agg = client.receipts.aggregate(
        gross=Sum("total_price", filter=RECOGNIZED),
        refunded=Sum("refunded_amount", filter=RECOGNIZED),
    )
    return (agg["gross"] or Decimal("0")) - (agg["refunded"] or Decimal("0"))


def client_debt(client) -> Decimal:
    """Сколько клиент должен = Σ долга по его чекам (Receipt.debt уже учитывает
    отмену/оплату/возвраты) + неоплаченный ВХОДЯЩИЙ долг на дату переезда из
    Excel (волна 2, `clients.opening`). Чеки — из prefetch, входящий долг —
    одним запросом. Эта же функция — у кассы (`credit`) и у кабинета клиента."""
    from .opening import opening_debt

    return sum((r.debt for r in client.receipts.all()), Decimal("0")) + opening_debt(client)


def client_change_due(client) -> Decimal:
    """Сколько ЦЕХ должен клиенту сдачей — зеркало долга.

    Клиент принёс больше, чем стоил заказ, а мелочи в кассе не было. Пока сдачу
    не отдали, эти деньги его, и на вопрос «сколько за нами осталось» отвечать
    должна система, а не память кассира.
    """
    return sum((r.change_due for r in client.receipts.all()), Decimal("0"))


class ClientSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)
    is_telegram_linked = serializers.BooleanField(read_only=True)
    has_password = serializers.BooleanField(read_only=True)
    referred_by_name = serializers.CharField(source="referred_by.display_name", read_only=True)
    referrals_count = serializers.SerializerMethodField()
    debt = serializers.SerializerMethodField()
    change_due = serializers.SerializerMethodField()
    orders_count = serializers.SerializerMethodField()
    # Дебиторка и аналитика (CLI-01, -03, -05, -10). `balance` — долг минус
    # сдача и аванс: плюс — клиент должен, минус — цех должен клиенту.
    effective_credit_limit = serializers.SerializerMethodField()
    advance_balance = serializers.SerializerMethodField()
    balance = serializers.SerializerMethodField()
    oldest_debt_at = serializers.SerializerMethodField()
    overdue_days = serializers.SerializerMethodField()
    last_order_at = serializers.SerializerMethodField()
    days_since_last_order = serializers.SerializerMethodField()
    margin = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            "id",
            "type",
            "full_name",
            "company_name",
            "phone",
            "inn",
            "telegram_chat_id",
            "display_name",
            "is_telegram_linked",
            "has_password",
            "referred_by",
            "referred_by_name",
            "referrals_count",
            # Постоянная скидка, % — касса подставляет её сама (2026-10-10).
            "discount_percent",
            # Лимит долга: свой (пусто — общий) и действующий (D-92).
            "credit_limit",
            "effective_credit_limit",
            "debt",
            "change_due",
            "advance_balance",
            "balance",
            "oldest_debt_at",
            "overdue_days",
            "last_order_at",
            "days_since_last_order",
            "margin",
            "orders_count",
            "created_at",
        ]
        read_only_fields = ["telegram_chat_id", "created_at"]

    # --- аналитика: из аннотаций списка, а без них (одна карточка) — расчётом ---
    def _metric(self, obj, name):
        if hasattr(obj, name):
            return getattr(obj, name)
        cache = getattr(obj, "_metrics_cache", None)
        if cache is None:
            from .analytics import metrics_for

            cache = obj._metrics_cache = metrics_for(obj)
        return cache[name]

    def get_effective_credit_limit(self, obj):
        # Общие настройки читаем один раз на запрос, а не на каждую строку списка.
        if obj.credit_limit is not None:
            return obj.credit_limit
        if "_default_limit" not in self.context:
            self.context["_default_limit"] = ClientSettings.load().default_credit_limit
        return self.context["_default_limit"]

    def get_advance_balance(self, obj):
        return self._metric(obj, "advance_total")

    def get_balance(self, obj):
        annotated = getattr(obj, "balance", None)
        if annotated is not None:
            return annotated
        return self.get_debt(obj) - self.get_change_due(obj) - self._metric(obj, "advance_total")

    def get_oldest_debt_at(self, obj):
        from .analytics import local_date

        day = local_date(self._metric(obj, "oldest_debt_at"))
        return day.isoformat() if day else None

    def get_overdue_days(self, obj):
        from .analytics import age_days

        return age_days(self._metric(obj, "oldest_debt_at"))

    def get_last_order_at(self, obj):
        from .analytics import local_date

        day = local_date(self._metric(obj, "last_order_at"))
        return day.isoformat() if day else None

    def get_days_since_last_order(self, obj):
        from .analytics import age_days

        return age_days(self._metric(obj, "last_order_at"))

    def get_margin(self, obj):
        return self._metric(obj, "margin_total")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Маржа — закупочная цифра: видят админ и бухгалтер, складовщик — нет.
        request = self.context.get("request")
        if not (request and getattr(request.user, "sees_money", False)):
            data.pop("margin", None)
        return data

    def validate_credit_limit(self, value):
        """Лимит долга задаёт только админ — как скидку (CLI-03)."""
        current = getattr(self.instance, "credit_limit", None)
        if value != current:
            request = self.context.get("request")
            if not (request and getattr(request.user, "is_admin_role", False)):
                raise serializers.ValidationError("Лимит долга задаёт только администратор.")
        return value

    def get_referrals_count(self, obj):
        # Вьюсет считает аннотацией (`referrals_total`); без неё — запросом.
        annotated = getattr(obj, "referrals_total", None)
        return annotated if annotated is not None else obj.referrals.count()

    def get_debt(self, obj):
        # Вьюсет считает долг аннотацией (её же использует сортировка по клику).
        # Fallback на Python нужен там, где клиент пришёл без аннотации —
        # например из вложенных сериализаторов.
        annotated = getattr(obj, "debt", None)
        return annotated if annotated is not None else client_debt(obj)

    def get_change_due(self, obj):
        annotated = getattr(obj, "change_due_total", None)
        return annotated if annotated is not None else client_change_due(obj)

    def get_orders_count(self, obj):
        annotated = getattr(obj, "orders_count", None)
        if annotated is not None:
            return annotated
        return obj.receipts.exclude(status="CANCELLED").count()

    def validate_referred_by(self, value):
        if value and self.instance and value.pk == self.instance.pk:
            raise serializers.ValidationError("Клиент не может привести сам себя.")
        # И не по кругу: «А привёл Б, Б привёл А» проверка на самого себя не
        # ловила, а по такой паре реферальные бонусы считаются в обе стороны и
        # отчёт по рефералам показывает двух клиентов, приведённых друг другом.
        # Идём вверх по цепочке рефереров — если упёрлись в самого клиента,
        # кольцо замкнулось. Ограничитель на длину — от битых данных, чтобы
        # проверка не крутилась вечно.
        if value and self.instance:
            seen, node, hops = {value.pk}, value.referred_by, 0
            while node is not None and hops < 50:
                if node.pk == self.instance.pk:
                    raise serializers.ValidationError(
                        f"«{value.display_name}» уже приведён этим клиентом — "
                        "получается кольцо."
                    )
                if node.pk in seen:
                    break
                seen.add(node.pk)
                node, hops = node.referred_by, hops + 1
        # Клиенту с историей заказов реферера проставляет только админ: иначе
        # складовщик «привязывал» давнего клиента к знакомому и начислял тому
        # бонус (CLI-07). Касса вызывает эту проверку и для клиента, найденного
        # по телефону, поэтому правило действует и при оформлении.
        if value and self.instance and self.instance.pk and self.instance.referred_by_id != value.pk:
            request = self.context.get("request")
            is_admin = bool(request and getattr(request.user, "is_admin_role", False))
            if not is_admin and self.instance.receipts.exists():
                raise serializers.ValidationError(
                    "Реферера клиенту с историей заказов ставит только администратор."
                )
        # Реферер зафиксирован после установки: складовщик его не меняет и не
        # очищает, админ — меняет напрямую в карточке. Очередь заявок на смену
        # убрана по просьбе владельца (2026-09-27): ей почти не пользовались.
        if self.instance and self.instance.referred_by_id is not None:
            if not value or value.pk != self.instance.referred_by_id:
                request = self.context.get("request")
                is_admin = bool(
                    request and getattr(request.user, "is_admin_role", False)
                )
                if not is_admin:
                    raise serializers.ValidationError(
                        "Реферал зафиксирован. Изменить его может только администратор."
                    )
        return value

    def validate_discount_percent(self, value):
        """Скидку клиента задаёт и меняет только админ (CLI-02). Складовщик
        её видит и применяет в кассе, но назначить её себе «по знакомству»
        не может — ни в карточке, ни при заведении клиента из кассы."""
        current = getattr(self.instance, "discount_percent", Decimal("0"))
        if value != current:
            request = self.context.get("request")
            if not (request and getattr(request.user, "is_admin_role", False)):
                raise serializers.ValidationError(
                    "Скидку клиента задаёт только администратор."
                )
        return value

    def validate_phone(self, value):
        """Тот же номер в другом написании — тот же клиент, а не новый.

        Уникальность на поле проверяет СТРОКУ, поэтому `0555 111 222` спокойно
        заводился поверх `+996555111222`, и один человек оказывался в списке
        дважды. Ловим это по цифрам и говорим, под кем номер уже записан.

        Телефон — ещё и ЛОГИН кабинета клиента, поэтому:
          * номер должен быть похож на номер: от 9 до 15 цифр (раньше проходило
            «1» и «абв»);
          * менять номер существующего клиента (другие цифры, а не другое
            написание) может только администратор.
        Незатронутый номер (в PATCH/PUT пришёл тот же, что в базе) не
        перепроверяем: у старых карточек бывают короткие номера, и править в них
        имя это не должно мешать.
        """
        from .phones import find_client_by_phone, only_digits, phone_key

        if self.instance is not None and value == self.instance.phone:
            return value
        digits = only_digits(value)
        if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
            raise serializers.ValidationError(
                f"Телефон: от {MIN_PHONE_DIGITS} до {MAX_PHONE_DIGITS} цифр, например "
                "+996 555 11 22 33."
            )
        if self.instance is not None and phone_key(value) != phone_key(self.instance.phone):
            request = self.context.get("request")
            if not (request and getattr(request.user, "is_admin_role", False)):
                raise serializers.ValidationError(
                    "Менять телефон клиента может только администратор: это логин его кабинета."
                )
        twin = find_client_by_phone(value)
        if twin and (self.instance is None or twin.pk != self.instance.pk):
            raise serializers.ValidationError(
                f"Этот номер уже записан за клиентом «{twin.display_name}» ({twin.phone})."
            )
        return value

    def update(self, instance, validated_data):
        """Смена телефона (логина кабинета) отзывает выданные клиенту токены."""
        from .phones import phone_key

        new_phone = validated_data.get("phone")
        if new_phone is not None and phone_key(new_phone) != phone_key(instance.phone):
            validated_data["credentials_version"] = (instance.credentials_version or 0) + 1
        return super().update(instance, validated_data)

    def validate(self, attrs):
        # Тип берём с запасным значением МОДЕЛИ, а не None. Раньше при создании
        # без явного `type` тут выходил None, обе проверки промахивались, и
        # карточка заводилась вообще без имени — а модель ставила PHYSICAL по
        # умолчанию. Дальше такая карточка запиралась: любая правка, даже одного
        # ИНН, упиралась в «Для физ. лица укажите ФИО».
        ctype = (
            attrs.get("type")
            or getattr(self.instance, "type", None)
            or Client.Type.PHYSICAL
        )
        if ctype == Client.Type.OSOO and not attrs.get(
            "company_name", getattr(self.instance, "company_name", None)
        ):
            raise serializers.ValidationError(
                {"company_name": "Для ОСОО укажите название компании."}
            )
        if ctype == Client.Type.PHYSICAL and not attrs.get(
            "full_name", getattr(self.instance, "full_name", None)
        ):
            raise serializers.ValidationError(
                {"full_name": "Для физ. лица укажите ФИО."}
            )
        return attrs


class ClientDetailSerializer(ClientSerializer):
    """Includes purchase statistics / LTV and the referral chain for CRM."""

    stats = serializers.SerializerMethodField()
    referrals = serializers.SerializerMethodField()
    orders = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()
    advances = serializers.SerializerMethodField()
    # Входящие остатки на дату переезда из Excel (волна 2): долг и аванс.
    opening_balances = serializers.SerializerMethodField()

    class Meta(ClientSerializer.Meta):
        fields = ClientSerializer.Meta.fields + [
            "stats",
            "referrals",
            "orders",
            "payments",
            "advances",
            "opening_balances",
        ]

    def get_opening_balances(self, obj):
        from .opening import balances_payload

        return balances_payload(obj.opening_balances.filter(reverted_at__isnull=True))

    def get_advances(self, obj):
        """Авансы клиента без заказа: сколько внёс, сколько ещё не зачтено."""
        return [
            {
                "id": a.id, "amount": a.amount, "remaining": a.remaining, "method": a.method,
                "method_display": a.get_method_display(), "paid_on": a.paid_on, "note": a.note,
                "reverted": a.reverted_at is not None,
            }
            for a in obj.advances.order_by("-paid_on", "-id")[:50]
        ]

    def get_payments(self, obj):
        """История оплат клиента: когда и сколько он реально принёс.

        По полю «оплачено» на чеке этого не видно — особенно после общей выплаты
        (одна сумма разошлась по нескольким заказам) и оплат задним числом.
        Период фильтрует по ДАТЕ ОПЛАТЫ, а не по дате заказа.
        """
        from sales.models import Payment

        from .models import BalanceOffset

        d_from = self.context.get("date_from")
        d_to = self.context.get("date_to")
        qs = Payment.objects.filter(receipt__client=obj).select_related("receipt")
        if d_from:
            qs = qs.filter(paid_on__gte=d_from)
        if d_to:
            qs = qs.filter(paid_on__lte=d_to)
        rows = [
            {
                "id": p.id,
                "amount": p.amount,
                "method": p.method,
                "method_display": p.get_method_display(),
                "paid_on": p.paid_on,
                "order_number": p.receipt.order_number,
                "order_title": p.receipt.title,
            }
            # Хвост истории обрезаем: у постоянного клиента это сотни строк,
            # а карточка показывает последние оплаты.
            for p in qs[:50]
        ]
        # Зачёт аванса в оплату заказа — тоже «оплата» в истории (деньги внесены
        # раньше, см. `ClientAdvance`): без него долг уменьшился, а в списке
        # оплат записи нет.
        uses = BalanceOffset.objects.filter(client=obj, source=BalanceOffset.Source.ADVANCE)
        if d_from:
            uses = uses.filter(used_on__gte=d_from)
        if d_to:
            uses = uses.filter(used_on__lte=d_to)
        rows += [
            {
                "id": f"adv{o.id}",
                "amount": o.amount,
                "method": "ADVANCE",
                "method_display": "Зачёт аванса",
                "paid_on": o.used_on,
                "order_number": o.order_number,
                "order_title": "",
            }
            for o in uses.order_by("-used_on", "-id")[:50]
        ]
        rows.sort(key=lambda r: r["paid_on"], reverse=True)
        return rows[:50]

    def get_orders(self, obj):
        """Заказы клиента (что он покупал) — для карточки CRM: номер, дата, сумма,
        статусы, долг и позиции. Receipt.Meta уже сортирует по -created_at.

        Если в списке выбран период, карточка показывает заказы того же периода —
        иначе фильтр «июль» открывал бы карточку со всей историей за год."""
        d_from = self.context.get("date_from")
        d_to = self.context.get("date_to")
        rows = []
        for r in obj.receipts.all():
            created = timezone.localtime(r.created_at).date()
            if (d_from and created < d_from) or (d_to and created > d_to):
                continue
            # Возвращённые строки тоже отдаём — с флагом: без них у
            # возвращённого заказа в карточке оставался голый итог «452 сом»
            # без единого намёка на возврат.
            items = [
                {
                    "title": (
                        i.material.name if i.material_id
                        else (i.service.name if i.service_id else "—")
                    ) + (f" — {i.note}" if i.note else ""),
                    "quantity": i.quantity,
                    # У возвращённой строки line_total = 0; для истории — что стоила.
                    "line_total": (
                        (i.quantity * i.price_per_item).quantize(Decimal("1"), rounding=ROUND_CEILING)
                        if i.is_returned else i.line_total
                    ),
                    "is_returned": i.is_returned,
                }
                for i in r.items.all()
            ]
            rows.append({
                "id": r.id,
                "order_number": r.order_number,
                "title": r.title,
                "created_at": r.created_at,
                # Нужен итогу за период в карточке: отменённый заказ не
                # считается в «заказов», как и в списке клиентов.
                "status": r.status,
                "total_price": r.total_price,
                # Сколько по заказу реально приняли. Нужно акту сверки: платёж,
                # принятый в момент продажи, записи `sales.Payment` не создаёт —
                # она заводится только при погашении долга. Без этой суммы акт
                # показывал бы все заказы неоплаченными.
                "amount_paid": r.amount_paid,
                "refunded_amount": r.refunded_amount,
                "payment_status": r.payment_status,
                "fulfillment_status": r.fulfillment_status,
                # Дата признания выручки: пусто у неоплаченного онлайн-счёта —
                # он не продажа и не долг (D-7). Акт сверки берёт в долг и
                # обороты только заказы с этой датой.
                "revenue_recognized_at": r.revenue_recognized_at,
                "debt": r.debt,
                "change_due": r.change_due,
                "items": items,
            })
        return rows

    def get_stats(self, obj):
        from sales.models import Receipt

        receipts = obj.receipts.all()
        # «Заказов» — как в списке клиентов: без отменённых (целиком возвращённых).
        # Раньше карточка считала все, а список — только живые, и у клиента с
        # одним возвращённым заказом стояло «1» в карточке и «—» в списке.
        # Возвращённые показаны отдельно, чтобы история не пропадала.
        agg = receipts.aggregate(
            orders=Count("id", filter=~Q(status=Receipt.Status.CANCELLED)),
            cancelled=Count("id", filter=Q(status=Receipt.Status.CANCELLED)),
            gross=Sum("total_price", filter=RECOGNIZED),
            refunded=Sum("refunded_amount", filter=RECOGNIZED),
        )
        gross = agg["gross"] or Decimal("0")
        refunded = agg["refunded"] or Decimal("0")
        return {
            "orders_count": agg["orders"] or 0,
            "cancelled_count": agg["cancelled"] or 0,
            "lifetime_value": gross - refunded,
            "gross": gross,
            "refunded": refunded,
        }

    def get_referrals(self, obj):
        """Кого привёл клиент и какой бонус за них начислен / выплачен / к выплате.

        Бонус — таблица начислений (`ReferralBonus`), а не «ставка × число
        привязок» (CLI-07): платим за приведённого с оплаченным и не
        возвращённым заказом, по ставке на момент начисления. Старые привязки
        показываются расчётом с пометкой `estimated` (D-95).
        """
        from .referrals import referral_summary

        return referral_summary(obj, client_ltv)


class ClientSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientSettings
        fields = ["default_credit_limit", "storekeeper_takes_debt"]


class ClientPriceSerializer(serializers.ModelSerializer):
    """Договорная цена клиента (волна 2): материал + единица ИЛИ работа
    (+ необязательно материал работы)."""

    service_name = serializers.CharField(source="service.name", read_only=True, default=None)
    material_name = serializers.CharField(source="material.name", read_only=True, default=None)
    sale_mode_display = serializers.CharField(source="get_sale_mode_display", read_only=True)

    class Meta:
        from .models import ClientPrice

        model = ClientPrice
        fields = [
            "id", "client", "service", "service_name", "material", "material_name",
            "sale_mode", "sale_mode_display", "price", "note", "updated_at",
        ]
        read_only_fields = ["updated_at"]
        validators = []          # уникальность — понятным текстом в validate()

    def validate(self, attrs):
        from .models import ClientPrice

        merged = {
            key: attrs.get(key, getattr(self.instance, key, None))
            for key in ("client", "service", "material", "sale_mode")
        }
        service, material, mode = merged["service"], merged["material"], merged["sale_mode"] or ""
        if service is None and material is None:
            raise serializers.ValidationError("Укажите услугу или материал.")
        if service is None and not mode:
            raise serializers.ValidationError({"sale_mode": "Для материала укажите единицу: кв.м, лист/штука или пог.м."})
        if service is not None and mode:
            raise serializers.ValidationError({"sale_mode": "У работы единицы нет — ставка как в каталоге услуги."})
        attrs["sale_mode"] = mode
        twin = ClientPrice.objects.filter(
            client=merged["client"], service=service, material=material, sale_mode=mode,
        )
        if self.instance is not None:
            twin = twin.exclude(pk=self.instance.pk)
        if twin.exists():
            raise serializers.ValidationError("Такая договорная цена у клиента уже есть — поправьте её.")
        return attrs
