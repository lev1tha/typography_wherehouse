from decimal import Decimal

from datetime import date, datetime

from django.db import transaction
from django.db.models import Count, F, ProtectedError
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdmin, IsAdminOrReadOnly, IsNotAccountant
from audit.models import AuditLog
from finance.periods import ensure_open

from .models import (
    InventoryLog,
    Material,
    MaterialImage,
    MaterialMonthOpening,
    MaterialType,
    ProductionSite,
    Roll,
    RollStocktake,
    Supplier,
    Supply,
)
from .rolls import InsufficientStock, receive_lot, stocktake_roll, write_off_roll
from .supplies import SupplyError, move_supply_date, post_supply, supply_summary, unpost_supply
from .waste import WasteError, waste_summary, write_off_waste
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
    QuickIntakeSerializer,
    RollSerializer,
    RollStocktakeInputSerializer,
    RollStocktakeSerializer,
    RollWriteOffSerializer,
    SupplierSerializer,
    SupplySerializer,
    WasteSerializer,
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

    # rolls — ради stock_value: он считается по остаткам партий, и без prefetch
    # каждый материал в списке уводил бы в отдельный запрос.
    queryset = Material.objects.prefetch_related("images", "rolls").all()
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
            qs = qs.filter(is_archived=True)
        else:
            qs = qs.filter(is_archived=False)
        # ?form=PIECE|SHEET|ROLL — форма материала так, как её видит человек:
        # штучный / лист / рулон. В базе это пара полей (`is_roll_material` +
        # `intake_form`), фильтровать по ним по отдельности бесполезно.
        form = self.request.query_params.get("form")
        if form == "PIECE":
            qs = qs.filter(is_roll_material=False)
        elif form in (Material.IntakeForm.SHEET, Material.IntakeForm.ROLL):
            qs = qs.filter(is_roll_material=True, intake_form=form)
        return qs

    def destroy(self, request, *args, **kwargs):
        """Удаление материала.

        Границу проводим по ПРОДАЖАМ, а не по любой истории. Раньше материал
        прятался (архивировался) уже из-за одного прихода — а это самый частый
        случай: завели материал, приняли поставку, увидели, что это дубль или
        опечатка. Из каталога он пропадал, но оставался в стоимости склада, в
        закупе материала и в отчёте по материалам: «удалил, а он в финансах».

        Продаж не было → удаляем НАСОВСЕМ вместе с партиями и складским
        журналом: ошибочная запись должна исчезнуть отовсюду, включая закуп
        (он считается по приходам).

        Продажи были → только прячем. Удалить нельзя: строки старых чеков
        ссылаются на материал, и суммы закрытых заказов поехали бы задним
        числом. Скрытый материал в расчёты периода тоже больше не лезет — см.
        фильтры `is_archived` в обзоре и отчёте по материалам.
        """
        material = self.get_object()
        name = material.name
        if not material.transaction_items.exists():
            lots = material.rolls.count()
            logs = material.inventory_logs.count()
            try:
                with transaction.atomic():
                    # PROTECT на обеих связях — убираем их своими руками и в
                    # одной транзакции, чтобы при отказе не осталось материала
                    # без партий.
                    material.rolls.all().delete()
                    material.inventory_logs.all().delete()
                    material.delete()
            except ProtectedError:
                # Материал держит что-то ещё (например, техкарта услуги) —
                # прятать безопаснее, чем ломать связь.
                pass
            else:
                AuditLog.record(
                    request.user,
                    f"Удалён материал «{name}» (партий: {lots}, движений: {logs})",
                )
                return Response(
                    {
                        "deleted": True,
                        "detail": (
                            "Материал удалён вместе с приходами — они ушли и из закупа."
                            if logs or lots
                            else "Товар удалён"
                        ),
                    },
                    status=status.HTTP_200_OK,
                )
        material.is_archived = True
        material.save(update_fields=["is_archived", "updated_at"])
        AuditLog.record(request.user, f"Материал «{name}» скрыт из каталога")
        return Response(
            {
                "archived": True,
                "detail": (
                    "Материал скрыт из каталога — по нему были продажи, "
                    "и удалить его нельзя: поехали бы суммы старых чеков. "
                    "В расчёты нового периода он больше не входит."
                ),
            },
            status=status.HTTP_200_OK,
        )

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
        serializer = QuickIntakeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ensure_open(serializer.validated_data.get("happened_on"), "Оформить поступление этой датой")
        data = serializer.validated_data
        # У площадного материала остаток лежит ДВАЖДЫ: числом в материале и
        # площадями партий, из которых FIFO берёт себестоимость. Быстрый приход
        # поднимал только число — партии не создавалось, и дальше себестоимость
        # начинала врать: продажа сверх площади партий уходила бесплатно, а
        # возврат такого чека надувал партии. Инвентаризация и списание эту
        # развилку держат, приход — не держал.
        #
        # Интерфейс сюда и не ведёт (модалка прихода зовёт `receive-roll`), но
        # эндпоинт открыт, и молча разъезжаться склад не должен.
        if data["material"].is_roll_material:
            return Response(
                {
                    "detail": (
                        f"«{data['material'].name}» приходит партией: нужны размеры "
                        "и стоимость закупки, иначе у материала не будет "
                        "себестоимости. Оформите поступление партией."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        # ШТУЧНЫЙ приход тоже заводит ПАРТИЮ (2026-08-27, просьба владельца).
        # Раньше он поднимал только число остатка, и партий у штучных не было
        # вовсе: новая закупочная цена молча переоценивала весь старый запас, а
        # в кассе нельзя было выбрать, из какой поставки берём. Для саморезов
        # это неважно, для дорогой смолы — уже нет.
        #
        # Цена за штуку обязательна: партия без неё дала бы себестоимость 0 и
        # завысила прибыль. Не указали — берём последнюю закупочную из карточки.
        qty = Decimal(data["quantity"])
        unit_cost = data.get("actual_price")
        if unit_cost in (None, ""):
            unit_cost = data["material"].purchase_price or Decimal("0")
        roll = receive_lot(
            data["material"],
            form=Roll.Form.PIECE,
            sheet_count=qty,
            purchase_cost=(Decimal(unit_cost) * qty).quantize(Decimal("0.01")),
            code=data.get("reason", "") or "",
            production=data["material"].production,
            user=request.user,
            received_at=_as_moment(data.get("happened_on")),
        )
        return Response(
            MaterialSerializer(roll.material, context={"request": request}).data
        )

    @action(detail=False, methods=["post"], permission_classes=[IsAdmin])
    def adjust(self, request):
        """POST /materials/adjust/ — inventory reconciliation."""
        serializer = AdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        material = data["material"]
        if material.sells_by_metre:
            # Рулон меряют рулеткой ПО РУЛОНАМ и в метрах — это акт промера
            # (`/rolls/<id>/stocktake/`). Общее число в кв.м для него не отвечает
            # ни на один вопрос, а расхождение эта ручка гнала бы через FIFO —
            # то есть списала бы полтора метра со СТАРЕЙШЕГО рулона, хотя
            # промеряли рулон №23. Единственный законный остаток для этой ручки —
            # «хвост сверх партий», и его сводит отдельное действие ниже.
            from .rolls import lots_area

            in_lots = lots_area(material)
            text = (
                f"«{material.name}» — рулонный материал: остаток сверяют промером "
                f"каждого рулона (кнопка «Промер» в Складе), а не общим числом в кв.м."
            )
            if material.quantity != in_lots:
                text += (
                    f" Остаток по материалу ({material.quantity} кв.м) расходится с "
                    f"суммой рулонов ({in_lots} кв.м) — сведите его кнопкой "
                    f"«Свести с рулонами»."
                )
            return Response({"detail": text}, status=status.HTTP_400_BAD_REQUEST)
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
                # Инвентаризация не «списывает», а ПРИРАВНИВАЕТ остаток к
                # пересчитанному: 0 ≤ counted проверил сериализатор, и защита
                # от ухода в минус здесь только мешала бы.
                allow_negative=True,
                log_type=InventoryLog.Type.ADJUSTMENT,
                reason=reason,
                user=request.user,
            )
        AuditLog.record(
            request.user,
            f"Инвентаризация «{material.name}»: расхождение {delta}",
        )
        return Response(MaterialSerializer(material, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="reconcile-lots", permission_classes=[IsAdmin])
    def reconcile_lots(self, request):
        """POST /materials/reconcile-lots/ {material} — свести остаток рулонного
        материала с суммой его партий.

        Только для материала, который продаётся метрами: у него правда лежит в
        рулонах (режут, промеряют и возвращают по рулонам), а число в карточке —
        производное. Хвост сверх партий там не списать ни продажей, ни промером,
        и без этой ручки он висел бы в остатке и стоимости склада вечно.
        """
        material = get_object_or_404(Material, pk=request.data.get("material"))
        if not material.sells_by_metre:
            return Response(
                {"detail": f"«{material.name}» не продаётся метрами — остаток правится инвентаризацией."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from .rolls import NothingToReconcile, reconcile_with_lots

        try:
            delta = reconcile_with_lots(material, user=request.user)
        except NothingToReconcile as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Сведение остатка «{material.name}» с рулонами: {delta:+} кв.м → {material.quantity} кв.м",
        )
        return Response(MaterialSerializer(material, context={"request": request}).data)

    @action(detail=False, methods=["post"], url_path="receive-roll", permission_classes=[IsAdmin])
    def receive_roll(self, request):
        """POST /materials/receive-roll/ — receive a lot (roll: ширина×длина,
        или лист: ширина×высота×кол-во) → площадь кв.м + себестоимость + наценка."""
        serializer = RollIntakeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ensure_open(serializer.validated_data.get("received_on"), "Принять партию этой датой")
        data = serializer.validated_data
        roll = receive_lot(
            data["material"],
            form=data["form"],
            width=data.get("width"),
            length=data.get("length"),
            height=data.get("height"),
            sheet_count=data.get("sheet_count"),
            # Площадь пришла напрямую (приём квадратами) — не считаем её из
            # размеров, их в этом способе и нет.
            area=data.get("area"),
            purchase_cost=data["purchase_cost"],
            received_at=_as_moment(data.get("received_on")),
            code=data.get("code", ""),
            production=data.get("production"),
            user=request.user,
            declared_length=data.get("declared_length"),
        )
        note = (
            f"Поступление «{roll.material.name}»: {roll.dimensions_label} = "
            f"{roll.initial_area} кв.м, {roll.purchase_cost} сом "
            f"(себест. {roll.cost_per_sqm}/кв.м)"
        )
        # Недолив — в журнал действий сразу: цифра, которую иначе никто не
        # сведёт, а рулон за рулоном она копится в чистый убыток.
        if roll.shortfall:
            note += (
                f". ЗАЯВЛЕНО {roll.declared_length} м, ПРИНЯТО {roll.length} м — "
                f"недостача {roll.shortfall} м"
            )
        AuditLog.record(request.user, note)
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
        if material.sells_by_metre:
            # Брак у рулона — это конкретный рулон и метры рулеткой, а не общее
            # число в кв.м: то шло FIFO со старейшего рулона, и «порвали 2 м
            # рулона №8» обнуляло целый №7. Списывают по рулону —
            # `/rolls/<id>/write-off/`.
            return Response(
                {
                    "detail": (
                        f"«{material.name}» — рулонный материал: списывают с "
                        f"конкретного рулона и в метрах (Склад → Движение → "
                        f"Списание → выберите рулон), а не общим числом в кв.м."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
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

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def stocktake(self, request, pk=None):
        """POST /rolls/<id>/stocktake/ — промер рулона рулеткой.

        Остаток партии приводится к намеренному, а расхождение остаётся АКТОМ:
        сколько было по системе, сколько намерили и почему разошлось. Правкой
        остатка так не выходит — там причина исчезает вместе с расхождением, и
        через месяц на вопрос «куда делись полтора метра» ответить нечем.
        """
        roll = self.get_object()
        serializer = RollStocktakeInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            act = stocktake_roll(
                roll,
                data["counted_metres"],
                reason_code=data["reason_code"],
                note=data.get("note", ""),
                user=request.user,
            )
        except InsufficientStock as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        label = act.roll.code or f"№{act.roll_id}"
        AuditLog.record(
            request.user,
            f"Промер рулона {label} «{act.roll.material.name}»: было "
            f"{act.expected_metres} м, намерено {act.counted_metres} м, "
            f"расхождение {act.difference} м ({act.get_reason_code_display()})"
            + (f". {act.note}" if act.note else ""),
        )
        return Response(RollStocktakeSerializer(act).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="write-off", permission_classes=[IsAdmin])
    def write_off(self, request, pk=None):
        """POST /rolls/<id>/write-off/ — списать метры С ЭТОГО рулона.

        Порча случается с конкретным рулоном на полке и меряется рулеткой —
        значит, и списывается по рулону, в метрах, его шириной и по его цене.
        Общее списание рулонного материала в кв.м (`/materials/write-off/`)
        для него закрыто: оно шло FIFO со старейшего рулона.
        """
        roll = self.get_object()
        serializer = RollWriteOffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        reason = serializer.reason_text()
        try:
            area = write_off_roll(roll, data["metres"], reason=reason, user=request.user)
        except InsufficientStock as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        label = roll.code or f"№{roll.pk}"
        # Себестоимость — метрами по цене метра этого рулона: в чём считал
        # поставщик, в том и списываем (площадь × цена за кв.м даёт копеечный
        # хвост округления).
        cost = (Decimal(str(data["metres"])) * roll.cost_per_pm).quantize(Decimal("0.01"))
        AuditLog.record(
            request.user,
            f"{reason} Рулон {label} «{roll.material.name}»: {data['metres']} м "
            f"({area} кв.м, себестоимость {cost} сом); "
            f"в рулоне осталось {roll.metres_remaining} м",
        )
        return Response(RollSerializer(roll).data)


class RollStocktakeViewSet(viewsets.ReadOnlyModelViewSet):
    """Акты промера рулонов — ТОЛЬКО чтение: акт это документ.

    Правка акта задним числом обессмыслила бы всю затею: расхождение должно
    остаться таким, каким его зафиксировали у рулона с рулеткой в руках.
    """

    queryset = RollStocktake.objects.select_related("roll__material", "created_by").all()
    serializer_class = RollStocktakeSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["roll", "reason_code"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        material = self.request.query_params.get("material")
        return qs.filter(roll__material_id=material) if material else qs


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
        # Прячем, а не удаляем, если на производство кто-то ссылается. Партии
        # проверяем НАРАВНЕ с материалами: у партии своё производство (эта пачка
        # из Китая, хотя обычно возим из Бишкека), и производство, на которое не
        # осталось материалов, вполне может держать историю приходов. Без этой
        # половины проверки `PROTECT` отдавал бы пятисотку.
        if site.materials.exists() or site.rolls.exists():
            site.is_archived = True
            site.save(update_fields=["is_archived"])
            return Response({"archived": True}, status=status.HTTP_200_OK)
        site.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SupplierViewSet(viewsets.ModelViewSet):
    """Справочник поставщиков.

    Заводит его тот, кто принимает товар: новая фирма всплывает в момент
    приёмки, и гонять складовщика к админу за строчкой справочника — верный
    способ получить накладную «без поставщика». Удалять — только админ.
    """

    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated, IsNotAccountant]
    pagination_class = None
    search_fields = ["name", "phone", "inn"]
    ordering = ["name"]

    def get_queryset(self):
        qs = Supplier.objects.prefetch_related("supplies__lines")
        if self.request.query_params.get("archived") == "1":
            return qs
        return qs.filter(is_archived=False)

    def destroy(self, request, *args, **kwargs):
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Удалять поставщиков может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        supplier = self.get_object()
        # Поставщик с накладными не удаляется, а скрывается: на нём висит
        # история поставок, и FK стоит на PROTECT.
        if supplier.supplies.exists():
            supplier.is_archived = True
            supplier.save(update_fields=["is_archived"])
            return Response(
                {"archived": True, "detail": "Поставщик скрыт — по нему есть накладные."},
                status=status.HTTP_200_OK,
            )
        supplier.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SupplyViewSet(viewsets.ModelViewSet):
    """Приходные накладные — поставка целиком, одним документом.

    Создаёт и видит их тот, кто принимает товар (админ и складовщик); отменяет
    только админ: отмена снимает материал с остатка.
    """

    serializer_class = SupplySerializer
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filterset_fields = ["supplier"]
    search_fields = ["number", "note", "supplier__name"]
    ordering = ["-received_on", "-id"]

    def get_queryset(self):
        qs = (
            Supply.objects.select_related("supplier", "created_by")
            .prefetch_related("lines__material")
        )
        d_from = self.request.query_params.get("date_from")
        d_to = self.request.query_params.get("date_to")
        if d_from:
            qs = qs.filter(received_on__gte=d_from)
        if d_to:
            qs = qs.filter(received_on__lte=d_to)
        if self.request.query_params.get("unpaid") == "1":
            qs = qs.filter(paid_amount__lt=F("stated_total"))
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Приход двигает закуп месяца — в закрытый период его не заводим.
        ensure_open(serializer.validated_data.get("received_on"), "Провести накладную этой датой")
        lines = serializer.validated_data.pop("lines", [])
        try:
            with transaction.atomic():
                supply = Supply.objects.create(
                    **serializer.validated_data, created_by=request.user
                )
                post_supply(supply, lines, user=request.user)
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        supply = self.get_queryset().get(pk=supply.pk)
        AuditLog.record(
            request.user,
            f"Приход по накладной {supply.number or f'#{supply.pk}'}"
            f"{f' от {supply.supplier.name}' if supply.supplier_id else ''}: "
            f"{supply.lines.count()} поз. на {supply.total_cost} сом",
        )
        return Response(
            self.get_serializer(supply).data, status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        """Состав накладной не правим — только её «бумажную» часть.

        Переписывать проведённые строки значило бы двигать склад задним числом:
        часть материала уже могла уйти в заказы. Ошиблись в позициях — отмените
        накладную и заведите заново; номер, сумма по бумаге и оплата правятся
        спокойно. ДАТА переносится вместе с партиями и движениями склада
        (`move_supply_date`) и держится замком периода с обеих сторон: раньше
        уезжал только закуп, а партии и журнал оставались в старом месяце.
        """
        request.data.pop("lines", None)
        supply = self.get_object()
        new_day = None
        raw = request.data.get("received_on")
        if raw not in (None, ""):
            try:
                new_day = date.fromisoformat(str(raw))
            except ValueError:
                return Response({"received_on": ["Некорректная дата."]}, status=status.HTTP_400_BAD_REQUEST)
        moved = new_day is not None and new_day != supply.received_on
        if moved:
            ensure_open(supply.received_on, "Перенести накладную из закрытого периода")
            ensure_open(new_day, "Перенести накладную в закрытый период")
        old_day = supply.received_on
        response = super().update(request, *args, **kwargs)
        if moved and response.status_code == 200:
            supply.refresh_from_db()
            move_supply_date(supply, new_day)
            AuditLog.record(
                request.user,
                f"Дата накладной {supply.number or f'#{supply.pk}'} перенесена: "
                f"{old_day:%d.%m.%Y} → {new_day:%d.%m.%Y} (партии и движения склада — за ней)",
            )
            response.data = self.get_serializer(self.get_queryset().get(pk=supply.pk)).data
        return response

    def destroy(self, request, *args, **kwargs):
        if not request.user.is_admin_role:
            return Response(
                {"detail": "Отменять накладные может только администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        supply = self.get_object()
        ensure_open(supply.received_on, "Отменить накладную закрытого периода")
        # Состав — ДО отмены: после неё от документа и его движений не остаётся
        # ничего, и журнал действий — единственное место, где видно, что сняли.
        summary = supply_summary(supply)
        try:
            unpost_supply(supply)
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(request.user, f"Отменена приходная накладная {summary}")
        return Response(status=status.HTTP_204_NO_CONTENT)


class WasteView(APIView):
    """POST /warehouse/waste/ — отход (брак): списать несколько строк разом,
    теми же мерками, что и приход (лист ширина × высота × штук, площадь, метры
    рулона, количество).

    Пишет и складовщик: брак видит тот, кто стоит у станка и принимает
    товар, — экран отходов стоит рядом с приёмкой. Бухгалтеру, как и всему,
    что двигает склад, закрыто. Замок периода отход не держит — как
    инвентаризацию и списание: они двигают только текущий остаток.
    """

    permission_classes = [IsAuthenticated, IsNotAccountant]

    def post(self, request):
        serializer = WasteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            entries = write_off_waste(
                data["lines"], user=request.user,
                happened_on=data.get("happened_on"), note=data.get("note", ""),
            )
        except (WasteError, InsufficientStock) as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        day = data.get("happened_on")
        AuditLog.record(
            request.user,
            "Отход/брак" + (f" за {day:%d.%m.%Y}" if day else "") + ": " + waste_summary(entries)
            + (f" ({data['note']})" if data.get("note") else ""),
        )
        return Response(
            InventoryLogSerializer(entries, many=True, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )
