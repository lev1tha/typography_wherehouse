import base64
import io
from decimal import Decimal

import qrcode
from rest_framework import serializers

from clients.models import Client
from services.models import PrintingService
from warehouse.models import Material, Roll

from .models import Receipt, TransactionItem


def _qr_data_uri(text: str) -> str:
    """Render `text` as a base64 PNG data URI for inline <img> display."""
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


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
    offcut_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    # Единица измерения строки — для печатных форм: в накладной и счёте колонка
    # «Ед.» обязательна, а вывести её на фронте не из чего: у резки количество
    # в погонных метрах, у листа — в штуках, у куска — в квадратных.
    unit_label = serializers.SerializerMethodField()
    # Код единицы — для перевода на фронте (`unit.*` в словарях): русская
    # подпись `unit_label` в кыргызском или английском документе торчала
    # чужим словом посреди переведённой таблицы.
    unit_code = serializers.SerializerMethodField()

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
            "offcut_area",
            "offcut_cost",
            "unit_label",
            "unit_code",
            "sale_mode",
            "width",
            "length",
            "is_returned",
        ]

    def get_cost_total(self, obj):
        return obj.cost_total if _is_admin(self.context) else None

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
        if obj.service_id and obj.service.uses_running_meter:
            return "METER"
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
        if obj.service_id and obj.service.uses_running_meter:
            return "пог.м"
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
            # Часть заказа, закрытая сдачей с прошлых заказов этого клиента.
            "change_applied",
            "payment_reference",
            "payment_url",
            "payment_qr",
            "cost_total",
            "margin",
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

    def get_margin(self, obj):
        return obj.margin if _is_admin(self.context) else None

    def get_payments(self, obj):
        """Принятые оплаты по заказу: когда и сколько. Дата может быть задним
        числом — общая выплата по клиенту проводится позже, чем берут деньги."""
        return [
            {
                "id": p.id,
                "amount": p.amount,
                "method": p.method,
                "paid_on": p.paid_on,
            }
            for p in obj.payments.all()
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
    width = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    length = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=0, required=False, allow_null=True
    )
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

    def validate(self, attrs):
        if attrs["type"] == TransactionItem.Type.MATERIAL and not attrs.get("material"):
            raise serializers.ValidationError("Для позиции материала укажите material.")
        if attrs["type"] == TransactionItem.Type.SERVICE and not attrs.get("service"):
            raise serializers.ValidationError("Для позиции услуги укажите service.")

        material = attrs.get("material")
        mode = attrs.get("mode")
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
            # Рулон продаётся ТОЛЬКО метрами: у него нет ни цены за кв.м, ни цены
            # за штуку, и площадь молча продала бы его за 0 сом (так и было при
            # «повторить заказ»: 1 пог.м превращался в 1 кв.м по нулевой цене).
            if material.sells_by_metre and mode != TransactionItem.SaleMode.METER:
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
        elif mode == TransactionItem.SaleMode.METER:
            raise serializers.ValidationError(
                f"«{material.name}» метрами не продаётся."
            )

        # Резка/монтаж по рулону: материал по площади сюда не идёт — у рулона
        # нет цены за кв.м, и строка материала ушла бы за 0 сом (дозаказ
        # предлагал любой материал по кв.м, включая рулоны). Метры продаются
        # отдельной строкой (METER), а работа реза по ним — строкой работы без
        # размеров куска: только длина реза и ставка.
        service = attrs.get("service")
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
        piecewise = material is not None and (
            attrs.get("mode") == TransactionItem.SaleMode.PIECE
            or material.unit == Material.Unit.PIECE
        )
        if piecewise and qty != qty.to_integral_value():
            unit = "листами" if material.is_roll_material else "штуками"
            raise serializers.ValidationError(
                f"«{material.name}» продаётся целыми {unit} — {qty} не получится."
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
        elif service is not None and service.uses_area:
            if attrs.get("cut_rate") is None:
                if service.uses_running_meter:
                    rate = service.rate_per_pm or (
                        material.cut_rate_per_pm if material else Decimal("0")
                    )
                    where = (
                        f"ни у станка «{service.name}», ни у материала «{material.name}»"
                        if material else f"у станка «{service.name}» (материал не выбран)"
                    )
                    if not rate or rate <= 0:
                        raise serializers.ValidationError(
                            f"Ставка резки не задана {where} — работа ушла бы в чек "
                            f"бесплатно. Задайте ставку в «Ценах и услугах» или в "
                            f"карточке материала (или админ укажет её вручную)."
                        )
                elif not service.rate_flat or service.rate_flat <= 0:
                    raise serializers.ValidationError(
                        f"«{service.name}»: не задана ставка работы за кв.м — задайте "
                        f"её в «Ценах и услугах» (или админ укажет вручную)."
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
    # «Клиент гасит и старый долг»: одной продажей и заказ оформляется, и долги
    # прошлых заказов закрываются. Раньше за этим приходилось идти в «Клиенты →
    # Погасить долг», то есть бросать наполовину собранный чек.
    pay_debt = serializers.BooleanField(required=False, default=False)
    # Дата заказа задним числом. Не указана — «сейчас». Право проверяет вьюха:
    # по этой дате считается вся отчётность, ставить её в прошлое может админ.
    order_date = serializers.DateField(required=False, allow_null=True)
    items = SaleItemInputSerializer(many=True)

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
