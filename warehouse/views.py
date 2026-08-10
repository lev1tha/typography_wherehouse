from decimal import Decimal

from datetime import datetime

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdmin, IsAdminOrReadOnly
from audit.models import AuditLog

from .models import (
    InventoryLog,
    Material,
    MaterialImage,
    MaterialMonthOpening,
    MaterialType,
    ProductionSite,
    Roll,
)
from .rolls import receive_lot
from .serializers import (
    build_ref_index,
    MaterialBulkRowSerializer,
    MaterialMonthOpeningSerializer,
    MaterialTypeSerializer,
    ProductionSiteSerializer,
    AdjustmentSerializer,
    InventoryLogSerializer,
    MaterialImageSerializer,
    MaterialPriceUpdateSerializer,
    MaterialSerializer,
    RollIntakeSerializer,
    RollSerializer,
    SupplySerializer,
    WriteOffSerializer,
)
from .stock import apply_stock_change


def _as_moment(day):
    """Дата из формы → момент времени. Поставку вносят задним числом, и её
    дата важнее момента ввода; не указана — берётся текущее время."""
    if not day:
        return None
    return timezone.make_aware(datetime.combine(day, datetime.min.time()))


class MaterialViewSet(viewsets.ModelViewSet):
    """Warehouse catalogue. Read for all staff; create/edit for admins.

    Supports ?search=<name|цвет|артикул>, ?ordering=name|quantity|price_per_unit
    and filters ?type=&color=&thickness_mm=, matching the warehouse screens.
    """

    queryset = Material.objects.prefetch_related("images").all()
    serializer_class = MaterialSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["type", "color", "thickness_mm", "production"]
    search_fields = ["name", "color", "article"]
    ordering_fields = ["name", "quantity", "price_per_unit", "purchase_price", "thickness_mm"]
    ordering = ["name"]

    def get_queryset(self):
        qs = super().get_queryset()
        # Скрытые материалы не показываем ни в каталоге, ни в кассе.
        # ?archived=1 — чтобы админ мог их увидеть и при желании вернуть.
        if self.request.query_params.get("archived") in ("1", "true", "True"):
            return qs.filter(is_archived=True)
        return qs.filter(is_archived=False)

    def destroy(self, request, *args, **kwargs):
        """Удаление материала. Если по нему уже была история (продажи, приход,
        партии) — не удаляем, а скрываем: иначе суммы в старых чеках и отчётах
        поехали бы задним числом. Товар без истории удаляется насовсем."""
        material = self.get_object()
        has_history = (
            material.transaction_items.exists()
            or material.inventory_logs.exists()
            or material.rolls.exists()
        )
        if has_history:
            material.is_archived = True
            material.save(update_fields=["is_archived", "updated_at"])
            AuditLog.record(request.user, f"Материал «{material.name}» скрыт из каталога")
            return Response(
                {
                    "archived": True,
                    "detail": "Материал скрыт из каталога — по нему есть история продаж или поступлений.",
                },
                status=status.HTTP_200_OK,
            )
        name = material.name
        material.delete()
        AuditLog.record(request.user, f"Удалён материал «{name}»")
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def restore(self, request, pk=None):
        """Вернуть скрытый материал в каталог.

        Ищем в ПОЛНОМ списке: обычная выборка прячет скрытые, и восстанавливать
        было бы нечего — приходил 404."""
        material = get_object_or_404(Material, pk=pk)
        material.is_archived = False
        material.save(update_fields=["is_archived", "updated_at"])
        AuditLog.record(request.user, f"Материал «{material.name}» возвращён в каталог")
        return Response(MaterialSerializer(material, context={"request": request}).data)

    @action(detail=True, methods=["patch"], url_path="update-price")
    def update_price(self, request, pk=None):
        """PATCH /materials/<id>/update-price/ — admin retail-price change."""
        if not request.user.is_admin_role:
            return Response({"error": "Только администратор может менять цену."},
                            status=status.HTTP_403_FORBIDDEN)
        material = self.get_object()
        serializer = MaterialPriceUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        old_price = material.price_per_unit
        material.price_per_unit = serializer.validated_data["price_per_unit"]
        material.save(update_fields=["price_per_unit", "updated_at"])
        AuditLog.record(
            request.user,
            f"Изменена розничная цена «{material.name}»: "
            f"{old_price} → {material.price_per_unit} сом",
        )
        return Response(MaterialSerializer(material, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="bulk", permission_classes=[IsAdmin])
    def bulk(self, request):
        """POST /materials/bulk/ — завести пачку материалов одним запросом.

        Заказчик пришёл из Excel, и его каталог — это полсотни строк. Заводить
        их модалкой по одной он не станет; сетка на фронте шлёт всё сюда.

        Всё или ничего: одна опечатка в 47-й строке не должна оставить в базе 46
        материалов, которых потом не найти. Ошибки возвращаются с номером
        строки, сетка подсвечивает ячейки, и он отправляет заново.
        """
        rows = request.data.get("rows")
        if not isinstance(rows, list) or not rows:
            return Response(
                {"detail": "Нет строк для сохранения."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Справочники и занятые названия — одним запросом на всю пачку, а не на
        # каждую строку. Регистр сводится в Python: в SQLite `iexact` не
        # складывает кириллицу, и «форекс» разошёлся бы с «Форекс» на деве.
        context = {
            "types": build_ref_index(MaterialType.objects.all()),
            "sites": build_ref_index(ProductionSite.objects.all()),
        }
        taken = {
            name.strip().casefold()
            for name in Material.objects.values_list("name", flat=True)
        }

        cleaned, errors, seen = [], [], set()
        for index, row in enumerate(rows):
            serializer = MaterialBulkRowSerializer(data=row, context=context)
            if not serializer.is_valid():
                errors.append({"row": index, "fields": serializer.errors})
                continue
            data = serializer.validated_data
            key = data["name"].strip().casefold()
            # Две разные беды с одинаковым исходом, но разными объяснениями:
            # такое название уже в каталоге — или строка задублирована в пачке.
            if key in taken:
                message = f"«{data['name']}» уже есть в каталоге."
            elif key in seen:
                message = f"«{data['name']}» повторяется в списке."
            else:
                seen.add(key)
                cleaned.append(data)
                continue
            errors.append({"row": index, "fields": {"name": [message]}})

        if errors:
            return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            created = [Material.objects.create(**data) for data in cleaned]
        AuditLog.record(request.user, f"Каталог пополнен пачкой: {len(created)} материалов")
        return Response(
            {
                "created": len(created),
                "materials": MaterialSerializer(
                    created, many=True, context={"request": request}
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], permission_classes=[IsAdmin])
    def supply(self, request):
        """POST /materials/supply/ — receive a new supply batch."""
        serializer = SupplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        material = apply_stock_change(
            data["material"],
            Decimal(data["quantity"]),
            log_type=InventoryLog.Type.SUPPLY,
            actual_price=data.get("actual_price"),
            reason=data.get("reason") or "Поступление от поставщика",
            user=request.user,
            happened_at=_as_moment(data.get("happened_on")),
        )
        return Response(MaterialSerializer(material, context={"request": request}).data)

    @action(detail=False, methods=["post"], permission_classes=[IsAdmin])
    def adjust(self, request):
        """POST /materials/adjust/ — inventory reconciliation."""
        serializer = AdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        material = data["material"]
        delta = Decimal(data["counted_quantity"]) - material.quantity
        reason = data.get("reason") or "Инвентаризация"
        if material.is_roll_material:
            # У рулонного материала остаток хранится ДВАЖДЫ: числом в материале и
            # площадями партий, из которых FIFO берёт себестоимость. Двигать
            # только число нельзя — они разъедутся, и дальше врать начнёт
            # себестоимость проданного: партии «знают» больше материала, чем есть.
            # Списание брака это уже делает правильно, инвентаризация — нет.
            from .rolls import consume_area, restore_area

            if delta < 0:
                consume_area(
                    material, -delta, user=request.user,
                    reason=reason, log_type=InventoryLog.Type.ADJUSTMENT,
                )
            elif delta > 0:
                restore_area(
                    material, delta, user=request.user,
                    reason=reason, log_type=InventoryLog.Type.ADJUSTMENT,
                )
            material.refresh_from_db()
        else:
            material = apply_stock_change(
                material,
                delta,
                log_type=InventoryLog.Type.ADJUSTMENT,
                reason=reason,
                user=request.user,
            )
        AuditLog.record(
            request.user,
            f"Инвентаризация «{material.name}»: расхождение {delta}",
        )
        return Response(MaterialSerializer(material, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="receive-roll", permission_classes=[IsAdmin])
    def receive_roll(self, request):
        """POST /materials/receive-roll/ — receive a lot (roll: ширина×длина,
        или лист: ширина×высота×кол-во) → площадь кв.м + себестоимость + наценка."""
        serializer = RollIntakeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        roll = receive_lot(
            data["material"],
            form=data["form"],
            width=data.get("width"),
            length=data.get("length"),
            height=data.get("height"),
            sheet_count=data.get("sheet_count"),
            purchase_cost=data["purchase_cost"],
            received_at=_as_moment(data.get("received_on")),
            code=data.get("code", ""),
            user=request.user,
        )
        AuditLog.record(
            request.user,
            f"Поступление «{roll.material.name}»: {roll.dimensions_label} = "
            f"{roll.initial_area} кв.м, {roll.purchase_cost} сом (себест. {roll.cost_per_sqm}/кв.м)",
        )
        return Response(
            MaterialSerializer(roll.material, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="write-off", permission_classes=[IsAdmin])
    def write_off(self, request):
        """POST /materials/write-off/ — write off stock (damage/defect/loss)."""
        serializer = WriteOffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        reason = serializer.reason_text()
        material = data["material"]
        if material.is_roll_material:
            from .rolls import consume_area
            consume_area(
                material, Decimal(data["quantity"]), user=request.user,
                reason=reason, log_type=InventoryLog.Type.WRITE_OFF,
            )
            material.refresh_from_db()
        else:
            material = apply_stock_change(
                material,
                -Decimal(data["quantity"]),
                log_type=InventoryLog.Type.WRITE_OFF,
                reason=reason,
                user=request.user,
            )
        AuditLog.record(
            request.user,
            f"{reason} «{material.name}» — {data['quantity']}",
        )
        return Response(MaterialSerializer(material, context={"request": request}).data)


class MaterialImageViewSet(viewsets.ModelViewSet):
    """Material photo gallery management (admin only for writes)."""

    queryset = MaterialImage.objects.all()
    serializer_class = MaterialImageSerializer
    permission_classes = [IsAdminOrReadOnly]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["material", "is_primary"]


class InventoryLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Журнал движений склада — лента «Движение».

    Фильтры: ?type= (приход/продажа/возврат/списание/корректировка), ?material=,
    ?year=&month=. Период НЕ обязателен: поставки вносят задним числом, и лента,
    по умолчанию обрезанная текущим месяцем, прятала бы их в момент ввода.

    Сортировка была по `created_at` — поля, которого у модели больше нет
    (переименовано в `happened_at`, когда приходы научились датироваться задним
    числом). Ни один экран журнал не показывал, поэтому 500 никто не замечал.
    """

    queryset = InventoryLog.objects.select_related(
        "material", "created_by", "receipt"
    ).all()
    serializer_class = InventoryLogSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["material", "type", "receipt"]
    search_fields = ["material__name", "reason"]
    ordering = ["-happened_at"]
    ordering_fields = ["happened_at", "quantity_changed", "material__name"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        def as_int(name):
            try:
                return int(params.get(name) or 0)
            except ValueError:
                return 0

        year, month = as_int("year"), as_int("month")
        if year:
            qs = qs.filter(happened_at__year=year)
        if month:
            qs = qs.filter(happened_at__month=month)
        return qs


class RollViewSet(viewsets.ReadOnlyModelViewSet):
    """Rolls (lots) of roll-materials — list & filter by material."""

    queryset = Roll.objects.select_related("material").all()
    serializer_class = RollSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["material"]
    ordering = ["received_at"]


class MaterialMonthOpeningViewSet(viewsets.ModelViewSet):
    """Остаток материала на начало месяца — ручной ввод, как в Excel заказчика.

    Создание делает upsert по (материал, год, месяц): фронт просто отправляет
    значение клетки, не выясняя предварительно, заводили её раньше или нет.
    """

    queryset = MaterialMonthOpening.objects.select_related("material")
    serializer_class = MaterialMonthOpeningSerializer
    permission_classes = [IsAdmin]
    filterset_fields = ["material", "year", "month"]
    pagination_class = None

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        row, _created = MaterialMonthOpening.objects.update_or_create(
            material=data["material"], year=data["year"], month=data["month"],
            defaults={"quantity": data["quantity"], "updated_by": request.user},
        )
        return Response(self.get_serializer(row).data, status=status.HTTP_200_OK)


class MaterialTypeViewSet(viewsets.ModelViewSet):
    """Справочник типов материала. Читают все, правит админ.

    Встроенный тип удалить нельзя; тип, на котором висят материалы, скрывается,
    а не удаляется — иначе каталог осиротел бы (FK стоит на PROTECT).
    """

    serializer_class = MaterialTypeSerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        qs = MaterialType.objects.annotate(materials_total=Count("materials"))
        if self.request.query_params.get("archived") == "1":
            return qs
        return qs.filter(is_archived=False)

    def destroy(self, request, *args, **kwargs):
        material_type = self.get_object()
        if material_type.is_builtin:
            return Response(
                {"detail": "Встроенный тип удалить нельзя — его можно скрыть."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if material_type.materials.exists():
            material_type.is_archived = True
            material_type.save(update_fields=["is_archived"])
            return Response({"archived": True}, status=status.HTTP_200_OK)
        material_type.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProductionSiteViewSet(viewsets.ModelViewSet):
    """Справочник производств («откуда возим»). Читают все, правит админ."""

    serializer_class = ProductionSiteSerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        qs = ProductionSite.objects.annotate(materials_total=Count("materials"))
        if self.request.query_params.get("archived") == "1":
            return qs
        return qs.filter(is_archived=False)

    def destroy(self, request, *args, **kwargs):
        site = self.get_object()
        if site.is_builtin:
            return Response(
                {"detail": "Встроенное производство удалить нельзя — его можно скрыть."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if site.materials.exists():
            site.is_archived = True
            site.save(update_fields=["is_archived"])
            return Response({"archived": True}, status=status.HTTP_200_OK)
        site.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
