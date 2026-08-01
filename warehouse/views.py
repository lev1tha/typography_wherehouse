from decimal import Decimal

from django.shortcuts import get_object_or_404

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdmin, IsAdminOrReadOnly
from audit.models import AuditLog

from .models import InventoryLog, Material, MaterialImage, Roll
from .rolls import receive_lot
from .serializers import (
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


class MaterialViewSet(viewsets.ModelViewSet):
    """Warehouse catalogue. Read for all staff; create/edit for admins.

    Supports ?search=<name> and ?ordering=name|quantity|price_per_unit|category
    and ?category=<value>, matching the storekeeper/admin warehouse screens.
    """

    queryset = Material.objects.prefetch_related("images").all()
    serializer_class = MaterialSerializer
    permission_classes = [IsAdminOrReadOnly]
    filterset_fields = ["category"]
    search_fields = ["name", "category"]
    ordering_fields = ["name", "quantity", "price_per_unit", "purchase_price", "category"]
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
        material = apply_stock_change(
            material,
            delta,
            log_type=InventoryLog.Type.ADJUSTMENT,
            reason=data.get("reason") or "Инвентаризация",
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
            markup_percent=data.get("markup_percent") or Decimal("0"),
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
    queryset = InventoryLog.objects.select_related("material", "created_by").all()
    serializer_class = InventoryLogSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["material", "type"]
    ordering = ["-created_at"]


class RollViewSet(viewsets.ReadOnlyModelViewSet):
    """Rolls (lots) of roll-materials — list & filter by material."""

    queryset = Roll.objects.select_related("material").all()
    serializer_class = RollSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["material"]
    ordering = ["received_at"]
