import base64
import io
from decimal import Decimal

import qrcode
from django.db.models import Sum
from rest_framework import serializers

from accounts.models import Employee
from clients.models import Client
from services.models import PrintingService
from services.pricing import resolve_rate
from warehouse.models import Material, Roll

from .models import Receipt, TransactionItem
# Площадь считаем ТОЙ ЖЕ функцией, что и сборщик строки: иначе «0.01 × 0.01»
# прошло бы проверку и легло в чек нулём после округления до трёх знаков.
from .sale_service import _area


def _qr_data_uri(text: str) -> str:
    """Render `text` as a base64 PNG data URI for inline <img> display."""
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def sells_roll_by_area(material) -> bool:
    """Рулон с ценой за кв.м изделия (CALC-10): второй способ продажи — SQM."""
    return material is not None and material.sells_roll_by_area


def _is_admin(context) -> bool:
    """Показывать ли закупочные цифры. Себестоимость и маржа — владельцу и
    бухгалтеру: складовщик оформляет и выдаёт заказы, но почём цех купил
    материал, ему знать незачем (те же правила, что у финансовых разделов).
    Бухгалтеру наоборот: эти цифры и есть его работа."""
    request = context.get("request")
    return bool(request and getattr(request.user, "sees_money", False))


class TransactionItemSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    service_name = serializers.CharField(source="service.name", read_only=True)
    line_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    cost_total = serializers.SerializerMethodField()
    # Из какого рулона резали — чтобы в чеке было видно «списано с рулона №7»,
    # а не только «списано со склада».
    roll_label = serializers.SerializerMethodField()
    # Обрезок: сколько списанного до клиента не дошло и во сколько это обошлось.
    offcut_area = serializers.DecimalField(max_digits=14, decimal_places=4, read_only=True)
    # Во сколько обрезок обошёлся — закупочная цифра, мастеру её не показываем
    # (STAFF-07), как и себестоимость строки.
    offcut_cost = serializers.SerializerMethodField()
    # Материал, под который посчитана работа, и станок — для «Наряда мастеру»,
    # печати и подписей в чеке (XL-11).
    work_material_name = serializers.CharField(source="work_material.name", read_only=True, default=None)
    machine = serializers.CharField(source="service.machine", read_only=True, default=None)
    machine_display = serializers.CharField(source="service.get_machine_display", read_only=True, default=None)
    # Единица измерения строки — для печатных форм: в накладной и счёте колонка
    # «Ед.» обязательна, а вывести её на фронте не из чего: у резки количество
    # в погонных метрах, у листа — в штуках, у куска — в квадратных.
    unit_label = serializers.SerializerMethodField()
    # Код единицы — для перевода на фронте (`unit.*` в словарях): русская
    # подпись `unit_label` в кыргызском или английском документе торчала
    # чужим словом посреди переведённой таблицы.
    unit_code = serializers.SerializerMethodField()
    # Правила прайса строки: «каталог → итог». `catalog_total` — сколько
    # строка стоила бы без правил; у строк до правил равна `line_total`
    # (точнее — стоимости строки, и у возвращённой тоже).
    catalog_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    sold_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    executor_name = serializers.CharField(source="executor.full_name", read_only=True, default=None)

    class Meta:
        model = TransactionItem
        fields = [
            "id",
            "type",
            "material",
            "material_name",
            "service",
            "service_name",
            "quantity",
            "price_per_item",
            "line_total",
            "cost_total",
            "roll",
            "roll_label",
            "used_width",
            # Рулон по площади изделия (CALC-10): размеры изделия — width×length.
            "roll_area",
            "offcut_area",
            "offcut_cost",
            "unit_label",
            "unit_code",
            "sale_mode",
            "width",
            "length",
            # Деталей, проходы и коэффициент толщины (CALC-02/-04), материал
            # работы, станок и выданное количество (XL-11, G1-N4).
            "parts_count",
            "passes",
            "thickness_coef",
            "work_material",
            "work_material_name",
            "machine",
            "machine_display",
            "issued_qty",
            # Выдано деталей (перепроверка 10.10, RU-N3): у строки с деталями
            # выдают штуками.
            "issued_parts",
            "price_is_manual",
            # Договорная цена клиента (волна 2): скидка к строке не применялась.
            "client_price",
            # Материал клиента и комментарий к работе: по ним строку без
            # материала узнают в чеке («Резка · акрил 3 мм клиента»).
            "own_material",
            "note",
            # Исполнитель работы (волна 2): id сотрудника и его ФИО.
            "executor",
            "executor_name",
            "is_returned",
            # Правила прайса (2026-10-10): цена до правил, минимум, проценты.
            "catalog_price",
            "catalog_total",
            "sold_total",
            "min_amount",
            "min_applied",
            "urgency_percent",
            "discount_percent",
        ]

    def get_cost_total(self, obj):
        return obj.cost_total if _is_admin(self.context) else None

    def get_offcut_cost(self, obj):
        return obj.offcut_cost if _is_admin(self.context) else None

    def get_roll_label(self, obj):
        """Человеческое имя партии: маркировка, если её писали, иначе номер."""
        if not obj.roll_id:
            return None
        return obj.roll.code or f"№{obj.roll_id}"

    def get_unit_code(self, obj):
        if obj.type == TransactionItem.Type.MATERIAL:
            if obj.sale_mode == TransactionItem.SaleMode.PIECE:
                return "PIECE"
            if obj.sale_mode == TransactionItem.SaleMode.METER:
                return "METER"
            if obj.material_id and obj.material.is_roll_material:
                return "SQM"
            return obj.material.unit if obj.material_id else "PIECE"
        # ОТХОДЫ: мерку выбрали при продаже и запомнили в `sale_mode` — у одной
        # услуги законно соседствуют квадраты листа, метры рулона и штуки.
        if obj.service_id and obj.service.uses_free_measure:
            return obj.sale_mode or TransactionItem.SaleMode.SQM
        if obj.service_id and obj.service.uses_running_meter:
            return "METER"
        # Площадные услуги без реза (гравировка, внутренний монтаж) —
        # количество строки и есть кв.м.
        if obj.service_id and obj.service.uses_area:
            return "SQM"
        return "PIECE"

    def get_unit_label(self, obj):
        if obj.type == TransactionItem.Type.MATERIAL:
            if obj.sale_mode == TransactionItem.SaleMode.PIECE:
                return "шт"
            if obj.sale_mode == TransactionItem.SaleMode.METER:
                return "пог.м"
            if obj.material_id and obj.material.is_roll_material:
                return "кв.м"
            return obj.material.get_unit_display() if obj.material_id else "шт"
        # Работа резки считается погонными метрами, остальные услуги — штуками
        # (буквы наружной установки) или разом за заказ.
        if obj.service_id and obj.service.uses_free_measure:
            return {
                TransactionItem.SaleMode.METER: "пог.м",
                TransactionItem.SaleMode.PIECE: "шт",
            }.get(obj.sale_mode, "кв.м")
        if obj.service_id and obj.service.uses_running_meter:
            return "пог.м"
        if obj.service_id and obj.service.uses_area:
            return "кв.м"
        return "шт"


class ReceiptSerializer(serializers.ModelSerializer):
    items = TransactionItemSerializer(many=True, read_only=True)
    client_name = serializers.CharField(source="client.display_name", read_only=True)
    cashier_name = serializers.CharField(source="cashier.username", read_only=True)
    cashier_role = serializers.CharField(source="cashier.get_role_display", read_only=True)
    payment_method_display = serializers.CharField(source="get_payment_method_display", read_only=True)
    has_service = serializers.BooleanField(read_only=True)
    debt = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    payment_qr = serializers.SerializerMethodField()
    # Себестоимость проданного по этому заказу и что от него осталось. Снимок
    # закупки на момент продажи — переоценка склада прошлые заказы не двигает.
    cost_total = serializers.SerializerMethodField()
    margin = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()
    # Сумма строк по каталогу, до правил прайса — для «каталог → итог».
    catalog_total = serializers.SerializerMethodField()
    # Гарантия/переделка: исходный заказ, во сколько переделки обошлись и маржа
    # исходного заказа после них — деньги, только тем, кто видит деньги.
    warranty_of_number = serializers.IntegerField(source="warranty_of.order_number", read_only=True, default=None)
    warranty_cost = serializers.SerializerMethodField()
    margin_net = serializers.SerializerMethodField()
    # Часть `change_applied`, закрытая АВАНСОМ клиента (волна 2): в окне чека
    # «зачтено сдачей» и «из аванса» — разными строками.
    advance_applied = serializers.SerializerMethodField()

    class Meta:
        model = Receipt
        fields = [
            "id",
            "order_number",
            "title",
            "client",
            "client_name",
            "cashier",
            "cashier_name",
            "cashier_role",
            "payment_method",
            "payment_method_display",
            "payment_status",
            "status",
            "fulfillment_status",
            "has_service",
            "total_price",
            "refunded_amount",
            "amount_paid",
            "debt",
            # Сдача, которую клиенту ещё не отдали — долг цеха перед ним.
            "change_due",
            # Часть заказа, закрытая сдачей с прошлых заказов этого клиента
            # (вместе с авансом; аванс отдельно — `advance_applied`).
            "change_applied",
            "advance_applied",
            "payment_reference",
            "payment_url",
            "payment_qr",
            "cost_total",
            "margin",
            # Правила прайса заказа (2026-10-10).
            "is_urgent",
            "urgency_percent",
            "discount_percent",
            "catalog_total",
            "is_warranty",
            "warranty_of",
            "warranty_of_number",
            "warranty_reason",
            "warranty_culprit",
            "warranty_cost",
            "margin_net",
            "buyer_name",
            "items",
            "payments",
            "created_at",
            "updated_at",
        ]

    def get_payment_qr(self, obj):
        # Only render a QR while an online payment is still awaiting settlement.
        if obj.payment_url and obj.payment_status == Receipt.PaymentStatus.PENDING:
            return _qr_data_uri(obj.payment_url)
        return None

    def get_cost_total(self, obj):
        return obj.cost_total if _is_admin(self.context) else None

    def get_catalog_total(self, obj):
        return sum((item.catalog_total for item in obj.items.all()), Decimal("0"))

    def get_margin(self, obj):
        return obj.margin if _is_admin(self.context) else None

    def get_warranty_cost(self, obj):
        return obj.warranty_cost if _is_admin(self.context) else None

    def get_margin_net(self, obj):
        return obj.margin_net if _is_admin(self.context) else None

    def get_advance_applied(self, obj):
        annotated = obj.__dict__.get("_advance_applied")
        if annotated is not None:
            return annotated
        if not obj.client_id or obj.change_applied <= 0:
            return Decimal("0")
        from clients.models import BalanceOffset

        return BalanceOffset.objects.filter(
            receipt=obj, source=BalanceOffset.Source.ADVANCE,
        ).aggregate(v=Sum("amount"))["v"] or Decimal("0")

    def get_payments(self, obj):
        """Принятые оплаты по заказу: когда и сколько. Дата может быть задним
        числом — общая выплата по клиенту проводится позже, чем берут деньги.

        Отменённая оплата остаётся своим днём (`cancelled`), отмена — встречной
        записью с минусом (`reversal`, D-158): отменять можно только живую."""
        from .sale_service import live_amounts

        payments = list(obj.payments.all())
        live = live_amounts(payments)
        return [
            {
                "id": p.id,
                "amount": p.amount,
                "method": p.method,
                "paid_on": p.paid_on,
                # Кто принял оплату и с каким примечанием: складовщик теперь
                # тоже принимает долг, админ видит это и может отменить.
                "note": p.note,
                "created_by_name": p.created_by.username if p.created_by_id else None,
                "reversal": p.amount < 0,
                "cancelled": p.amount > 0 and live.get(p.pk, p.amount) <= 0,
            }
            for p in payments
        ]


class SaleItemInputSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=TransactionItem.Type.choices)
    material = serializers.PrimaryKeyRelatedField(
        queryset=Material.objects.all(), required=False, allow_null=True
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=PrintingService.objects.all(), required=False, allow_null=True
    )
    # Три знака, как у колонки `TransactionItem.quantity`: продажа по площади
    # шлёт сюда площадь куска (0.554 кв.м), и с двумя знаками такой заказ
    # отклонялся «не более 2 цифры после запятой».
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=0, required=False, default=0
    )
    # Из какого физического рулона режем. Не указан — берём початый (FIFO).
    roll = serializers.PrimaryKeyRelatedField(
        queryset=Roll.objects.all(), required=False, allow_null=True
    )
    # Ширина, которая реально уходит клиенту, когда она уже рулона: полосу
    # 0.5 м от рулона 0.9 отрезают на всю ширину, и 0.4 остаётся обрезком цеха.
    # Не указана — считаем, что ушло всё.
    used_width = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # Material sale mode: PIECE = whole sheet/roll at piece_price; SQM = by area.
    mode = serializers.ChoiceField(
        choices=["PIECE", "SQM", "METER"], required=False, allow_null=True
    )
    # Cutting / area-service: dimensions (width × length = area).
    # Размеры детали — три знака после запятой (0.455 м): два знака округляли
    # деталь до сантиметра и меняли цену (CALC-06 / XL-08).
    width = serializers.DecimalField(
        max_digits=8, decimal_places=3, min_value=0, required=False, allow_null=True
    )
    length = serializers.DecimalField(
        max_digits=8, decimal_places=3, min_value=0, required=False, allow_null=True
    )
    # «Деталей, шт»: площадь и длина реза умножаются на число одинаковых деталей.
    parts_count = serializers.IntegerField(min_value=1, max_value=1000, required=False, default=1)
    # Проходов гравировки/реза: ставка за кв.м умножается на их число.
    passes = serializers.IntegerField(min_value=1, max_value=20, required=False, default=1)
    # Cutting only: length of the cut in running metres (drives the work price).
    running_meters = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # Optional manual price overrides (default to the material's catalogue prices).
    material_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    cut_rate = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # МАТЕРИАЛ КЛИЕНТА: работа без материала со склада. Строка — только цена
    # резки/гравировки × сколько отрезано, что резали — в `note`.
    own_material = serializers.BooleanField(required=False, default=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)
    # Исполнитель работы (волна 2, STAFF-02): сотрудник цеха. Не выбран —
    # выработку разбирает ведомость по-старому (учётка кассира, станок).
    executor = serializers.PrimaryKeyRelatedField(
        queryset=Employee.objects.all(), required=False, allow_null=True
    )

    def validate_executor(self, value):
        if value is not None and not value.is_active:
            raise serializers.ValidationError("Сотрудник отключён — выберите работающего.")
        return value

    def to_internal_value(self, data):
        # Неизвестные поля позиции — отказ, а не молчание: `materials=[…]` или
        # `price_per_item` раньше уходили в никуда, и заказ оформлялся не так,
        # как его набрали (G4-N2, CALC-07).
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({
                    key: "Неизвестное поле позиции: оно не учитывается при расчёте."
                    for key in unknown
                })
        return super().to_internal_value(data)

    def _reject_unused(self, attrs, service, material):
        """Поля, которые для этой позиции ничего не значат, — отказ с причиной.

        Раньше они принимались и молча терялись: `quantity=12` у реза с
        размерами давал цену одной детали, ставка у монтажа — каталожную цену,
        размеры у штучной услуги — просто пропадали. Кассир был уверен, что
        заказ собран так, как он ввёл, а он был собран иначе.
        """
        def given(key):
            return attrs.get(key) not in (None, "", False)

        is_material = attrs["type"] == TransactionItem.Type.MATERIAL
        area_service = service is not None and service.uses_area
        # Рулон по площади изделия (CALC-10): размеры — ширина и длина ИЗДЕЛИЯ,
        # площадь из них считает сервер.
        roll_area = is_material and attrs.get("mode") == TransactionItem.SaleMode.SQM and sells_roll_by_area(material)
        errors = {}
        if is_material:
            for key in ("width", "length", "running_meters", "cut_rate", "own_material"):
                if roll_area and key in ("width", "length"):
                    continue
                if given(key):
                    errors[key] = "У материала этого поля нет — размеры куска задаёт услуга реза."
            if roll_area:
                if given("used_width"):
                    errors["used_width"] = (
                        "У изделия из рулона по кв.м ширина изделия — поле width; used_width — "
                        "только у продажи метрами."
                    )
                if given("width") and given("length") and Decimal(str(attrs.get("quantity") or 0)) > 0:
                    errors["quantity"] = (
                        "Площадь изделия считается из ширины и длины (width × length) — "
                        "количество не указывается."
                    )
            if attrs.get("parts_count", 1) != 1 or attrs.get("passes", 1) != 1:
                errors["parts_count"] = "«Деталей» и «проходы» бывают только у реза и гравировки."
            if given("executor"):
                errors["executor"] = "Исполнитель бывает только у работы, не у материала."
        else:
            if given("used_width"):
                errors["used_width"] = "Ширина изделия — только у рулона, проданного метрами."
            if not area_service and not (service and service.uses_free_measure):
                for key in ("width", "length", "running_meters"):
                    if given(key):
                        errors[key] = "У этой услуги размеров нет: цена за штуку или фикс."
            if area_service:
                if attrs.get("parts_count", 1) > 1 and not (given("width") and given("length")):
                    errors["parts_count"] = "Несколько деталей считаются от размеров: укажите ширину и длину."
                if given("width") and given("length") and Decimal(str(attrs.get("quantity") or 0)) > 0:
                    errors["quantity"] = (
                        "При размерах количество не используется — число одинаковых "
                        "деталей задаёт поле parts_count."
                    )
                if given("running_meters") and not service.uses_running_meter:
                    errors["running_meters"] = "Длина реза бывает только у резки."
            else:
                if attrs.get("parts_count", 1) != 1 or attrs.get("passes", 1) != 1:
                    errors["parts_count"] = "«Деталей» и «проходы» бывают только у реза и гравировки."
            if given("mode") and not (service and service.uses_free_measure):
                errors["mode"] = "Мерка (mode) задаётся только у отходов."
        if errors:
            raise serializers.ValidationError(errors)

    def validate(self, attrs):
        if attrs["type"] == TransactionItem.Type.MATERIAL and not attrs.get("material"):
            raise serializers.ValidationError("Для позиции материала укажите material.")
        if attrs["type"] == TransactionItem.Type.SERVICE and not attrs.get("service"):
            raise serializers.ValidationError("Для позиции услуги укажите service.")

        material = attrs.get("material")
        mode = attrs.get("mode")
        service = attrs.get("service")
        self._reject_unused(attrs, service, material)

        # Материал клиента — только у площадной услуги (резка, гравировка) и
        # БЕЗ материала со склада: две правды об одном куске («чужой» и «наш»)
        # разъехались бы в остатке. Не угадываем, что имелось в виду, — отказ.
        if attrs.get("own_material"):
            if attrs["type"] != TransactionItem.Type.SERVICE or service is None or not service.uses_area:
                raise serializers.ValidationError(
                    "«Материал клиента» бывает только у резки и гравировки."
                )
            if material is not None:
                raise serializers.ValidationError(
                    f"«{service.name}»: материал клиента — не выбирайте материал со "
                    f"склада, иначе он спишется. Уберите одно из двух."
                )
        # ОТХОДЫ: мерка — ЯВНАЯ, материала со склада нет.
        #
        # Отход уже списан там, где его признали браком или где он остался
        # обрезком от резки; выбранный здесь материал списался бы ВТОРОЙ раз и
        # увёл остаток в минус. Мерку не угадываем по цифрам: 2 у отходов
        # рулона — это метры, у отходов листа — квадраты, и молчаливая подмена
        # посчитала бы заказ не по тому прайсу.
        if attrs["type"] == TransactionItem.Type.SERVICE and service is not None and service.uses_free_measure:
            if material is not None:
                raise serializers.ValidationError(
                    f"«{service.name}»: материал со склада не выбирают — отход "
                    f"уже списан, второй раз он уйдёт в минус."
                )
            if not mode:
                raise serializers.ValidationError(
                    f"«{service.name}»: укажите мерку — площадь (SQM), длина "
                    f"(METER) или штуки (PIECE)."
                )

        # Способ продажи материала по площади (лист / кв.м / рулон) — ЯВНЫЙ.
        # Раньше отсутствующий `mode` молча становился «кв.м», и дозаказ «1 лист»
        # уходил в чек как 1 кв.м по цене за квадрат (1 250 вместо 3 700, со
        # склада 1 кв.м вместо 2.98), а «2 рулона» — как 2 кв.м по цене за кв.м,
        # которой у рулона нет (0 сом). Штучный материал (крепёж, клей) режимов
        # не имеет — у него одна цена за единицу, и `mode` ему не нужен.
        if attrs["type"] == TransactionItem.Type.MATERIAL and material.is_roll_material:
            if not mode:
                raise serializers.ValidationError(
                    f"«{material.name}»: укажите способ продажи — лист (PIECE), "
                    f"площадь (SQM) или длина (METER)."
                )
            # Рулон продаётся метрами: у него нет цены за штуку, и площадь без
            # цены за кв.м молча продала бы его за 0 сом (так и было при
            # «повторить заказ»: 1 пог.м превращался в 1 кв.м по нулевой цене).
            #
            # CALC-10 (D-140): если у рулона задана цена за кв.м, второй способ —
            # площадь ИЗДЕЛИЯ: ширина × длина × цена за кв.м (баннер 1×2 по 220 =
            # 440), а со склада — вся ширина × длина. Размеры изделия
            # обязательны: без длины не узнать, сколько рулона отрезали.
            if material.sells_by_metre and mode == TransactionItem.SaleMode.SQM and sells_roll_by_area(material):
                width, length = attrs.get("width"), attrs.get("length")
                if not (width and length and width > 0 and length > 0):
                    raise serializers.ValidationError(
                        f"«{material.name}» по кв.м изделия: укажите ширину и длину изделия, м "
                        f"(width и length) — по длине со склада уходит рулон."
                    )
                roll = attrs.get("roll")
                full = roll.width if roll is not None and roll.width else material.roll_width
                if full and width > full:
                    raise serializers.ValidationError(
                        f"«{material.name}»: изделие {width} м шире рулона {full} м — так не "
                        f"отрезать. Продайте полосы метрами или выберите рулон шире."
                    )
            elif material.sells_by_metre and mode != TransactionItem.SaleMode.METER:
                raise serializers.ValidationError(
                    f"«{material.name}» продаётся погонными метрами: укажите длину "
                    f"(режим METER), а не площадь или штуки."
                )
            # И наоборот: метров нет ни у листа, ни у штучного материала.
            if mode == TransactionItem.SaleMode.METER and not material.sells_by_metre:
                raise serializers.ValidationError(
                    f"«{material.name}» метрами не продаётся — укажите лист (PIECE) "
                    f"или площадь (SQM)."
                )
        elif attrs["type"] == TransactionItem.Type.MATERIAL and mode == TransactionItem.SaleMode.METER:
            raise serializers.ValidationError(
                f"«{material.name}» метрами не продаётся."
            )

        # Резка/монтаж по рулону: материал по площади сюда не идёт — у рулона
        # нет цены за кв.м, и строка материала ушла бы за 0 сом (дозаказ
        # предлагал любой материал по кв.м, включая рулоны). Метры продаются
        # отдельной строкой (METER), а работа реза по ним — строкой работы без
        # размеров куска: только длина реза и ставка.
        if (
            attrs["type"] == TransactionItem.Type.SERVICE
            and service is not None
            and service.uses_material
            and material is not None
            and material.sells_by_metre
            and (attrs.get("width") or attrs.get("length") or attrs.get("quantity"))
        ):
            raise serializers.ValidationError(
                f"«{material.name}» продаётся погонными метрами: оформите метры "
                f"отдельной строкой, а работу реза — без размеров куска (только "
                f"длина реза)."
            )

        # Штуки — целые. «2,5 листа» или «2,5 крепежа» не бывает, а система их
        # спокойно продавала и списывала, оставляя на складе дробный хвост.
        # Килограммы, литры и метры дробными быть могут — их не трогаем.
        qty = Decimal(str(attrs.get("quantity") or 0))
        if (
            attrs["type"] == TransactionItem.Type.MATERIAL
            and mode == TransactionItem.SaleMode.SQM
            and sells_roll_by_area(material)
        ):
            # Рулон по кв.м изделия: количество строки — площадь изделия.
            qty = _area(attrs["width"], attrs["length"])
        piecewise = material is not None and (
            attrs.get("mode") == TransactionItem.SaleMode.PIECE
            or material.unit == Material.Unit.PIECE
        )
        if piecewise and qty != qty.to_integral_value():
            unit = "листами" if material.is_roll_material else "штуками"
            raise serializers.ValidationError(
                f"«{material.name}» продаётся целыми {unit} — {qty} не получится."
            )
        # Буквы и прочие услуги за штуку — тоже целым числом (RP-N8): «2,5
        # буквы» продавались и уходили в чек половиной ставки.
        if (
            attrs["type"] == TransactionItem.Type.SERVICE and service is not None
            and (service.uses_pieces or (service.uses_free_measure and mode == TransactionItem.SaleMode.PIECE))
            and qty != qty.to_integral_value()
        ):
            raise serializers.ValidationError(
                f"«{service.name}» считается штуками — {qty} не получится, только целое число."
            )

        # Резка без длины реза — это работа за ноль. Длину кривой при фигурном
        # резе вводит мастер руками, и пустое поле молча уезжало в чек нулём:
        # материал посчитан, а самая дорогая работа цеха — бесплатно. Пустоту
        # здесь отклоняем, а не подставляем: площадь вместо длины уже пробовали,
        # кв.м считались как пог.м, и работа выходила втрое дешевле. Ставка при
        # этом может быть нулевой (подарок) — это осознанное решение админа,
        # а не забытое поле.
        if (
            attrs["type"] == TransactionItem.Type.SERVICE
            and service is not None
            and service.uses_running_meter
            and Decimal(str(attrs.get("running_meters") or 0)) <= 0
        ):
            raise serializers.ValidationError(
                f"«{service.name}»: укажите длину реза в пог.м — без неё работа "
                f"уйдёт в чек бесплатно."
            )

        # НЕЯВНЫЙ ноль цены не проходит. Явный — законный подарок админа
        # (`material_price=0`, `cut_rate=0`); а вот когда цену никто не
        # называл и в каталоге её тоже нет, строка молча уходила в чек за 0:
        # у станков ставка резки 0 («берётся у материала»), у нового материала
        # ставки нет — и вся резка по нему бесплатна, а касса складовщику даже
        # строку «Работа» не показывала. Пустой каталог — это ошибка ввода, а не
        # скидка; сообщаем, чего не хватает, и кому это исправить.
        if attrs["type"] == TransactionItem.Type.MATERIAL:
            if qty <= 0:
                raise serializers.ValidationError(
                    f"«{material.name}»: укажите количество больше нуля."
                )
            if attrs.get("material_price") is None:
                if mode == TransactionItem.SaleMode.PIECE:
                    price = material.piece_price_for_qty(qty)
                    if not price and not material.is_roll_material:
                        price = material.price_per_unit
                    what = f"цена за {'лист' if material.is_roll_material else 'штуку'}"
                elif mode == TransactionItem.SaleMode.METER:
                    price = material.price_per_pm
                    what = "цена за пог.м"
                else:
                    price = material.sqm_price if material.is_roll_material else material.price_per_unit
                    what = "цена за кв.м" if material.is_roll_material else "цена за единицу"
                if not price or price <= 0:
                    raise serializers.ValidationError(
                        f"«{material.name}»: в каталоге не задана {what} — задайте "
                        f"её в карточке материала (или админ укажет цену вручную)."
                    )
        elif service is not None and service.uses_free_measure:
            # ОТХОДЫ. Мерка уже проверена выше; здесь — количество и цена.
            # Площадь можно прислать готовой (`quantity`) или размерами:
            # обрезок мерят рулеткой, и «0.8 × 1.2» удобнее, чем 0.96.
            if mode == TransactionItem.SaleMode.SQM and attrs.get("width") and attrs.get("length"):
                qty = _area(attrs["width"], attrs["length"])
            if qty <= 0:
                what = {
                    TransactionItem.SaleMode.METER: "длину в пог.м",
                    TransactionItem.SaleMode.PIECE: "количество штук",
                }.get(mode, "размеры (ширина × длина) или площадь")
                raise serializers.ValidationError(
                    f"«{service.name}»: укажите {what} — иначе строка уйдёт в чек нулём."
                )
            # Цена: вписанная в кассе или каталожная ДЛЯ ЭТОЙ мерки. Пустая
            # каталожная — не скидка, а незаполненный справочник: молчаливый
            # ноль здесь отдал бы отход даром.
            if attrs.get("cut_rate") is None:
                price, what = {
                    TransactionItem.SaleMode.METER: (service.rate_per_pm, "цена за пог.м"),
                    TransactionItem.SaleMode.PIECE: (service.rate_per_piece, "цена за штуку"),
                }.get(mode, (service.rate_flat, "цена за кв.м"))
                if not price or price <= 0:
                    raise serializers.ValidationError(
                        f"«{service.name}»: не задана {what} — впишите цену в окне "
                        f"или задайте её в «Ценах и услугах»."
                    )
        elif service is not None and service.uses_area:
            # Гравировка и прочие площадные услуги без реза считаются ОТ
            # ПЛОЩАДИ: без размеров (или готовой площади) строка ушла бы в чек
            # нулём — так же, как рез без длины.
            if (
                not service.uses_running_meter
                and not (attrs.get("width") and attrs.get("length"))
                and qty <= 0
            ):
                raise serializers.ValidationError(
                    f"«{service.name}»: укажите размеры (ширина × длина) — по "
                    f"площади считается цена."
                )
            if attrs.get("cut_rate") is None:
                if service.uses_running_meter:
                    # Ставка — матрица / станок / материал (+ коэффициент толщины):
                    # одна функция с кассой (`services.pricing.resolve_rate`).
                    rate = resolve_rate(service, material).rate
                    if attrs.get("own_material"):
                        # Чужой материал: ставки материала нет по определению,
                        # цену называет тот, кто оформляет, — и складовщик тоже.
                        where = f"у станка «{service.name}», а у материала клиента её нет"
                        fix = "Впишите цену резки в окне."
                    else:
                        where = (
                            f"ни у станка «{service.name}», ни у материала «{material.name}»"
                            if material else f"у станка «{service.name}» (материал не выбран)"
                        )
                        fix = (
                            "Задайте ставку в «Ценах и услугах» или в карточке "
                            "материала (или админ укажет её вручную)."
                        )
                    if not rate or rate <= 0:
                        raise serializers.ValidationError(
                            f"Ставка резки не задана {where} — работа ушла бы в чек "
                            f"бесплатно. {fix}"
                        )
                elif resolve_rate(service, material).rate <= 0:
                    fix = (
                        "впишите цену за кв.м в окне или задайте её в «Ценах и услугах»"
                        if service.staff_sets_rate
                        else "задайте её в «Ценах и услугах» (или админ укажет вручную)"
                    )
                    raise serializers.ValidationError(
                        f"«{service.name}»: не задана ставка работы за кв.м — {fix}."
                    )
            # Материал куска — по площади: без цены за кв.м он тоже ушёл бы за 0.
            if (
                service.uses_material
                and material is not None
                and attrs.get("width")
                and attrs.get("length")
                and attrs.get("material_price") is None
                and not material.sqm_price
            ):
                raise serializers.ValidationError(
                    f"«{material.name}»: в каталоге не задана цена за кв.м — материал "
                    f"куска ушёл бы в чек за 0."
                )
        elif service is not None and service.uses_pieces:
            # Наружная установка: цена только за букву из каталога (ручной
            # цены у этой строки нет), нулевая — это пустой справочник.
            if attrs.get("cut_rate") is None and (not service.rate_per_piece or service.rate_per_piece <= 0):
                raise serializers.ValidationError(
                    f"«{service.name}»: не задана ставка за букву — задайте её в "
                    f"«Ценах и услугах», иначе установка уйдёт в чек бесплатно."
                )
        elif service is not None:
            # Фиксированные услуги («Прочее», установка): цена — из каталога.
            if attrs.get("cut_rate") is None and (not service.base_price or service.base_price <= 0):
                raise serializers.ValidationError(
                    f"«{service.name}»: не задана фиксированная цена — задайте её "
                    f"в «Ценах и услугах», иначе услуга уйдёт в чек бесплатно."
                )
        return attrs


class SaleCreateSerializer(serializers.Serializer):
    """Checkout payload. `client_id` for an existing client, or `client` dict
    to create one inline (live phone search drives the choice on the frontend).
    """

    client_id = serializers.PrimaryKeyRelatedField(
        queryset=Client.objects.all(), required=False, allow_null=True
    )
    client = serializers.DictField(required=False)
    payment_method = serializers.ChoiceField(choices=Receipt.PaymentMethod.choices)
    # Необязательное название заказа — показывается в списке чеков.
    title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    amount_paid = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    # «Вся сумма»: клиент платит ровно столько, сколько выйдет. Сумму чека знает
    # только сервер после сборки строк, кассе её угадывать нельзя — она считала
    # 978 там, где сервер насчитал 979, и полностью оплаченный заказ повисал с
    # долгом в сом. Флаг важнее `amount_paid`.
    pay_full = serializers.BooleanField(required=False, default=False)
    # «Зачесть сдачу»: остаток заказа закрывается сдачей с прошлых заказов этого
    # же клиента. Деньги за неё уже в кассе, поэтому она и не входит в
    # `amount_paid`, который присылает касса.
    use_change = serializers.BooleanField(required=False, default=False)
    # «Зачесть аванс» (волна 2, D-93): остаток заказа после сдачи закрывается
    # авансом клиента. Деньги аванса уже в кассе — касса не двигается.
    use_advance = serializers.BooleanField(required=False, default=False)
    # «Клиент гасит и старый долг»: одной продажей и заказ оформляется, и долги
    # прошлых заказов закрываются. Раньше за этим приходилось идти в «Клиенты →
    # Погасить долг», то есть бросать наполовину собранный чек.
    pay_debt = serializers.BooleanField(required=False, default=False)
    # Дата заказа задним числом. Не указана — «сейчас». Право проверяет вьюха:
    # по этой дате считается вся отчётность, ставить её в прошлое может админ.
    order_date = serializers.DateField(required=False, allow_null=True)
    # Правила прайса заказа (2026-10-10). «Срочно» — наценка из настроек цен.
    # Скидка не прислана — берётся из карточки клиента; другую (или 0 —
    # снять) задаёт только админ, проверяет вьюха.
    is_urgent = serializers.BooleanField(required=False, default=False)
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        required=False, allow_null=True,
    )
    # Имя покупателя для заказа В ДОЛГ без карточки клиента: долг без имени
    # взыскать не с кого (CLI-03).
    buyer_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    # Коды предупреждений, которые кассир подтвердил («да, строка на 120 000»).
    confirmed_warnings = serializers.ListField(
        child=serializers.CharField(max_length=40), required=False, default=list,
    )
    # Гарантия / переделка (G2-N3): заказ за счёт цеха со ссылкой на исходный.
    is_warranty = serializers.BooleanField(required=False, default=False)
    warranty_of = serializers.PrimaryKeyRelatedField(
        queryset=Receipt.objects.all(), required=False, allow_null=True,
    )
    warranty_reason = serializers.CharField(required=False, allow_blank=True, max_length=255)
    warranty_culprit = serializers.CharField(required=False, allow_blank=True, max_length=120)
    # Заказ оформлен из коммерческого предложения: КП помечается «заказ оформлен».
    quote_id = serializers.IntegerField(required=False, allow_null=True)
    items = SaleItemInputSerializer(many=True)

    def to_internal_value(self, data):
        # Неизвестные поля заказа — отказ (RP-N8), как и у позиций: опечатка
        # `is_urgnet` молча давала заказ без срочности.
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - set(self.fields))
            if unknown:
                raise serializers.ValidationError({
                    key: "Неизвестное поле заказа: оно не учитывается при оформлении."
                    for key in unknown
                })
        return super().to_internal_value(data)

    def validate(self, attrs):
        if attrs.get("is_warranty"):
            if not (attrs.get("warranty_reason") or "").strip():
                raise serializers.ValidationError(
                    {"warranty_reason": "Укажите причину переделки."}
                )
        elif attrs.get("warranty_of") or attrs.get("warranty_reason") or attrs.get("warranty_culprit"):
            raise serializers.ValidationError(
                {"is_warranty": "Исходный заказ, причина и виновник бывают только у гарантийного заказа."}
            )
        return attrs

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Добавьте хотя бы одну позицию.")
        # Позиция «содержательна», если в ней есть количество ИЛИ размеры куска
        # (у резки количество не передаётся вовсе — там ширина×длина). Заказ, где
        # везде нули, это промах по кнопке: раньше он молча заводил чек на 0 сом,
        # и такие пустышки оседали в списке чеков и в статистике.
        # Нулевая ЦЕНА при этом законна — подарок или бесплатная доработка.
        def has_content(item):
            if (item.get("quantity") or 0) > 0:
                return True
            if (item.get("width") or 0) > 0 and (item.get("length") or 0) > 0:
                return True
            return (item.get("running_meters") or 0) > 0

        if not any(has_content(item) for item in value):
            raise serializers.ValidationError(
                "В заказе нет ни одной позиции с количеством или размером."
            )
        return value

    def validate_order_date(self, value):
        from django.utils import timezone

        # Будущим числом заказ не оформляют — этой работы ещё не было.
        if value and value > timezone.localdate():
            raise serializers.ValidationError("Дата заказа не может быть в будущем.")
        return value


class RefundSerializer(serializers.Serializer):
    item_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True
    )
    # С какого счёта отдали деньги (cash-02): пусто — с того, куда они пришли
    # (зачтённый аванс — обратно в аванс); «ADVANCE» — на аванс клиента (RM-N7).
    method = serializers.ChoiceField(
        choices=list(Receipt.PaymentMethod.choices) + [("ADVANCE", "На аванс клиента")],
        required=False, allow_null=True, allow_blank=True,
    )
    # Причина возврата — обязательна для не-админа, если заказ оплачен (STAFF-08);
    # пишется в журнал действий.
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)
    # Частичный возврат КОЛИЧЕСТВА строки (cash-09, волна 2): [{id, quantity}].
    # Часть отделяется в новую строку и возвращается она; сумма заказа не
    # меняется ни на сом (`sale_service.split_line_for_refund`).
    quantities = serializers.ListField(child=serializers.DictField(), required=False, allow_empty=True)

    def validate_quantities(self, value):
        out = {}
        for row in value:
            if set(row) - {"id", "quantity"} or "id" not in row or "quantity" not in row:
                raise serializers.ValidationError("Каждая позиция: {id, quantity}.")
            if row["id"] in out:
                raise serializers.ValidationError("Строка указана дважды.")
            out[row["id"]] = row["quantity"]
        return out

    def to_internal_value(self, data):
        # Лишние поля — отказ. `quantity` раньше принимался и молча возвращал
        # строку целиком (cash-09); часть количества — полем `quantities`.
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                hint = (
                    " Часть количества строки возвращается полем quantities: "
                    "[{id, quantity}]."
                    if "quantity" in unknown else ""
                )
                raise serializers.ValidationError({
                    key: f"Неизвестное поле возврата.{hint}" for key in unknown
                })
        return super().to_internal_value(data)
