"""API полки остатков (2026-10-11, D-200…D-205).

`/api/warehouse/leftovers/`:
- GET — что лежит (по умолчанию — на полке и проданное частично), фильтры:
  `material`, `site`, `measure`, `status` (ON_SHELF / PARTIAL / SOLD /
  WRITTEN_OFF / ALL), `age_min` (дней), `min_width` + `min_length` (подойдёт ли
  кусок — в любом повороте), `q` (материал, примечание, номер заказа),
  `source_receipt`. Самые старые — сверху.
- POST — положить (складовщик и админ; бухгалтер только смотрит). Один
  остаток или `{"items": [...]}` разом — из карточки заказа.
- GET `<id>/` — карточка с историей продаж.
- POST `<id>/write-off/` — списать (выбросили): админ, с причиной.
- GET `summary/` — по материалам: кусков, площадь, метры, самый старый.
- GET `sales/` — проданное за период: когда, по какому чеку, за сколько.
- GET `export/` — CSV («;», запятая, BOM).
- GET `from-receipt/?receipt=` — материалы строк заказа для «Остатки на полку».
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsAdmin, IsNotAccountant
from sales import reporting
from sales.models import Receipt, TransactionItem

from .exports import QTY, cell, csv_response, num
from .leftovers import LeftoverRejected, check_size, put_on_shelf
from .leftovers import write_off as write_off_leftover
from .models import Leftover, Material, ProductionSite

ZERO = Decimal("0")
AVAILABLE = (Leftover.Status.ON_SHELF, Leftover.Status.PARTIAL)


def _live_sales():
    return Prefetch(
        "sold_items",
        queryset=TransactionItem.objects.filter(is_returned=False).only(
            "id", "leftover_id", "quantity", "price_per_item", "is_returned",
        ),
        to_attr="live_sales",
    )


def _age_days(moment) -> int:
    return max((timezone.localdate() - timezone.localtime(moment).date()).days, 0)


class LeftoverSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True, default=None)
    source_receipt_number = serializers.IntegerField(
        source="source_receipt.order_number", read_only=True, default=None,
    )
    created_by_name = serializers.CharField(source="created_by.username", read_only=True, default=None)
    written_off_by_name = serializers.CharField(source="written_off_by.username", read_only=True, default=None)
    label = serializers.CharField(read_only=True)
    size_text = serializers.CharField(read_only=True)
    sold_pieces = serializers.SerializerMethodField()
    sold_amount = serializers.SerializerMethodField()
    piece_area = serializers.SerializerMethodField()
    area_left = serializers.SerializerMethodField()
    metres_left = serializers.SerializerMethodField()
    age_days = serializers.SerializerMethodField()

    class Meta:
        model = Leftover
        fields = [
            "id", "material", "material_name", "site", "site_name", "measure", "width", "length",
            "size_text", "label", "pieces", "pieces_left", "sold_pieces", "written_off_pieces",
            "status", "piece_area", "area_left", "metres_left", "sold_amount",
            "source_receipt", "source_receipt_number", "note", "created_by_name", "created_at",
            "age_days", "written_off_at", "written_off_by_name", "write_off_reason",
        ]

    def get_sold_pieces(self, obj):
        return obj.pieces - obj.pieces_left - obj.written_off_pieces

    def get_sold_amount(self, obj):
        lines = getattr(obj, "live_sales", None)
        if lines is None:
            lines = obj.sold_items.filter(is_returned=False)
        return sum((line.sold_total for line in lines), ZERO)

    def get_piece_area(self, obj):
        return obj.piece_area.quantize(Decimal("0.0001"))

    def get_area_left(self, obj):
        return (obj.piece_area * obj.pieces_left).quantize(Decimal("0.0001"))

    def get_metres_left(self, obj):
        if obj.measure != Leftover.Measure.METER or not obj.length:
            return ZERO
        return (obj.length * obj.pieces_left).quantize(Decimal("0.001"))

    def get_age_days(self, obj):
        return _age_days(obj.created_at)


class LeftoverInputSerializer(serializers.Serializer):
    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    site = serializers.PrimaryKeyRelatedField(
        queryset=ProductionSite.objects.all(), required=False, allow_null=True,
    )
    measure = serializers.ChoiceField(choices=Leftover.Measure.choices)
    width = serializers.DecimalField(max_digits=8, decimal_places=3, required=False, allow_null=True)
    length = serializers.DecimalField(max_digits=8, decimal_places=3, required=False, allow_null=True)
    pieces = serializers.IntegerField(min_value=1, max_value=100000)
    source_receipt = serializers.PrimaryKeyRelatedField(
        queryset=Receipt.objects.all(), required=False, allow_null=True,
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        try:
            check_size(attrs["measure"], attrs.get("width"), attrs.get("length"))
        except LeftoverRejected as e:
            raise serializers.ValidationError(str(e))
        return attrs


def _decimal(raw):
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw).replace(",", "."))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _sale_row(line) -> dict:
    receipt = line.receipt
    sold_at = receipt.revenue_recognized_at or receipt.created_at
    return {
        "id": line.id,
        "receipt": str(receipt.pk),
        "order_number": receipt.order_number,
        "sold_at": sold_at,
        "leftover": line.leftover_id,
        "label": line.leftover.label,
        "material": line.leftover.material_id,
        "material_name": line.leftover.material.name,
        "pieces": int(line.quantity),
        "price": line.price_per_item,
        "total": line.sold_total,
        "is_returned": line.is_returned,
        "returned_at": line.returned_at,
    }


class LeftoverViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    """Полка остатков: смотреть — все (бухгалтер тоже), класть — склад и
    админ, списывать — админ."""

    serializer_class = LeftoverSerializer
    # Фильтры свои (`_filtered`): размер с поворотом и «доступное по
    # умолчанию» стандартными бэкендами не выразить.
    filter_backends = []

    def get_permissions(self):
        if self.action == "write_off":
            return [IsAdmin()]
        if self.request.method not in ("GET", "HEAD", "OPTIONS"):
            return [IsAuthenticated(), IsNotAccountant()]
        return [IsAuthenticated()]

    def get_queryset(self):
        return Leftover.objects.select_related(
            "material", "site", "source_receipt", "created_by", "written_off_by",
        ).prefetch_related(_live_sales())

    def _filtered(self, qs=None):
        p = self.request.query_params
        qs = self.get_queryset() if qs is None else qs
        for key in ("material", "site", "measure", "source_receipt"):
            if p.get(key):
                qs = qs.filter(**{key: p[key]})
        state = (p.get("status") or "").upper()
        if state == "ALL":
            pass
        elif state in Leftover.Status.values:
            qs = qs.filter(status=state)
        else:
            qs = qs.filter(status__in=AVAILABLE)
        try:
            age = int(p.get("age_min") or 0)
        except ValueError:
            age = 0
        if age > 0:
            qs = qs.filter(created_at__lt=timezone.now() - timedelta(days=age))
        w, l = _decimal(p.get("min_width")), _decimal(p.get("min_length"))
        if w is not None or l is not None:
            w, l = w or ZERO, l or ZERO
            # Кусок подходит, если влезает деталь w × l — как угодно повёрнутая.
            # У пог.м ширины нет: важна длина.
            qs = qs.filter(
                Q(width__gte=w, length__gte=l) | Q(width__gte=l, length__gte=w)
                | Q(measure=Leftover.Measure.METER, length__gte=max(w, l))
            )
        q = (p.get("q") or "").strip()
        if q:
            # Варианты регистра — для SQLite: её icontains не складывает
            # кириллицу («оракал» не находил «Оракал»); на Postgres не мешает.
            cond = Q()
            for variant in {q, q.lower(), q.upper(), q.capitalize()}:
                cond |= Q(material__name__icontains=variant) | Q(note__icontains=variant)
            if q.lstrip("№").isdigit():
                cond |= Q(source_receipt__order_number=int(q.lstrip("№")))
            qs = qs.filter(cond)
        return qs.order_by("created_at", "id")

    def list(self, request, *args, **kwargs):
        queryset = self._filtered()
        page = self.paginate_queryset(queryset)
        data = self.get_serializer(page if page is not None else queryset, many=True).data
        return self.get_paginated_response(data) if page is not None else Response(data)

    def retrieve(self, request, *args, **kwargs):
        leftover = self.get_object()
        data = dict(self.get_serializer(leftover).data)
        lines = (
            TransactionItem.objects.filter(leftover=leftover)
            .select_related("receipt", "leftover__material").order_by("id")
        )
        data["sales"] = [_sale_row(line) for line in lines]
        return Response(data)

    def create(self, request, *args, **kwargs):
        many = isinstance(request.data, dict) and isinstance(request.data.get("items"), list)
        raw = request.data["items"] if many else [request.data]
        if not raw:
            return Response({"detail": "Добавьте хотя бы один остаток."}, status=status.HTTP_400_BAD_REQUEST)
        serializer = LeftoverInputSerializer(data=raw, many=True)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                made = [put_on_shelf(**row, user=request.user) for row in serializer.validated_data]
        except LeftoverRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        out = LeftoverSerializer(self.get_queryset().filter(pk__in=[m.pk for m in made]).order_by("id"),
                                 many=True).data
        if many:
            return Response({"results": out}, status=status.HTTP_201_CREATED)
        return Response(out[0], status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="write-off")
    def write_off(self, request, pk=None):
        leftover = self.get_object()
        try:
            write_off_leftover(leftover.pk, reason=request.data.get("reason") or "", user=request.user)
        except LeftoverRejected as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(LeftoverSerializer(self.get_queryset().get(pk=leftover.pk)).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """По материалам: кусков, площадь (кв.м), метры (пог.м), сколько
        позиций и самый старый кусок. Самые залежавшиеся — сверху."""
        rows = {}
        for lo in self._filtered().filter(pieces_left__gt=0):
            row = rows.setdefault(lo.material_id, {
                "material": lo.material_id, "material_name": lo.material.name, "positions": 0,
                "pieces": 0, "area": ZERO, "metres": ZERO, "oldest": lo.created_at,
            })
            row["positions"] += 1
            row["pieces"] += lo.pieces_left
            row["area"] += lo.piece_area * lo.pieces_left
            if lo.measure == Leftover.Measure.METER and lo.length:
                row["metres"] += lo.length * lo.pieces_left
            row["oldest"] = min(row["oldest"], lo.created_at)
        out = sorted(rows.values(), key=lambda r: (r["oldest"], r["material_name"]))
        for row in out:
            row["area"] = row["area"].quantize(Decimal("0.0001"))
            row["metres"] = row["metres"].quantize(Decimal("0.001"))
            row["oldest_days"] = _age_days(row["oldest"])
        return Response({
            "rows": out,
            "totals": {
                "positions": sum(r["positions"] for r in out),
                "pieces": sum(r["pieces"] for r in out),
                "area": sum((r["area"] for r in out), ZERO),
                "metres": sum((r["metres"] for r in out), ZERO),
            },
        })

    @action(detail=False, methods=["get"])
    def sales(self, request):
        """Проданное с полки за период — тем же правилом, что выручка
        (`sales.reporting`): продажа — днём признания выручки, возврат — днём
        возврата. `net` — то, что полка принесла в выручку периода."""
        p = request.query_params
        d_from, d_to = parse_date(p.get("date_from") or ""), parse_date(p.get("date_to") or "")
        shelf = {"leftover__isnull": False}
        if p.get("material"):
            shelf["leftover__material"] = p["material"]
        sold = reporting._between(
            reporting.sold_lines(TransactionItem.objects.filter(is_returned=False, **shelf)),
            reporting.LINE_SOLD_ON, d_from, d_to,
        )
        back = reporting.added_back(d_from, d_to).filter(**shelf)
        out = reporting.returned_lines(d_from, d_to).filter(**shelf)
        related = ("receipt", "leftover__material")
        plus = list(sold.select_related(*related)) + list(back.select_related(*related))
        minus = list(out.select_related(*related))
        rows = {line.id: _sale_row(line) for line in plus + minus}
        totals = {
            "sold": sum((line.sold_total for line in plus), ZERO),
            "returned": sum((line.sold_total for line in minus), ZERO),
        }
        totals["net"] = totals["sold"] - totals["returned"]
        totals["pieces"] = int(sum((line.quantity for line in plus), ZERO) - sum((line.quantity for line in minus), ZERO))
        by_material = defaultdict(lambda: {"revenue": ZERO, "pieces": 0})
        for sign, lines in ((1, plus), (-1, minus)):
            for line in lines:
                slot = by_material[line.leftover.material.name]
                slot["revenue"] += sign * line.sold_total
                slot["pieces"] += sign * int(line.quantity)
        return Response({
            "rows": sorted(rows.values(), key=lambda r: (r["sold_at"], r["id"]), reverse=True),
            "totals": totals,
            "materials": [
                {"name": name, **slot}
                for name, slot in sorted(by_material.items(), key=lambda kv: -kv[1]["revenue"])
            ],
        })

    @action(detail=False, methods=["get"])
    def export(self, request):
        status_names = dict(Leftover.Status.choices)
        measure_names = {"SQM": "кв.м", "METER": "пог.м", "PIECE": "шт"}

        def rows():
            yield [
                "№", "Материал", "Площадка", "Мерка", "Ширина, м", "Длина, м", "Положено, шт",
                "На полке, шт", "Продано, шт", "Списано, шт", "Площадь на полке, кв.м",
                "Пог.м на полке", "Продано на сумму", "Статус", "Заказ", "Примечание",
                "Положил", "Положили", "Дней на полке", "Причина списания",
            ]
            for lo in self._filtered():
                data = LeftoverSerializer(lo).data
                yield [
                    str(lo.id), lo.material.name, lo.site.name if lo.site_id else "",
                    measure_names.get(lo.measure, lo.measure),
                    num(lo.width, QTY), num(lo.length, QTY), str(lo.pieces), str(lo.pieces_left),
                    str(data["sold_pieces"]), str(lo.written_off_pieces),
                    num(data["area_left"], QTY), num(data["metres_left"], QTY), num(data["sold_amount"]),
                    str(status_names.get(lo.status, lo.status)),
                    str(lo.source_receipt.order_number or "") if lo.source_receipt_id else "",
                    lo.note, data["created_by_name"] or "", cell(lo.created_at), str(data["age_days"]),
                    lo.write_off_reason,
                ]

        return csv_response(rows(), f"polka-ostatkov-{timezone.localdate():%Y-%m-%d}.csv")

    @action(detail=False, methods=["get"], url_path="from-receipt")
    def from_receipt(self, request):
        """Что предложить в «Остатки на полку» из карточки заказа: материалы
        строк заказа (материал и материал работы) и что уже положили."""
        try:
            receipt = Receipt.objects.get(pk=request.query_params.get("receipt"))
        except (Receipt.DoesNotExist, ValueError, TypeError, DjangoValidationError):
            return Response({"detail": "Заказ не найден."}, status=status.HTTP_404_NOT_FOUND)
        seen, materials = set(), []
        for line in receipt.items.select_related("material", "work_material").order_by("id"):
            for material in (line.material, line.work_material):
                if material is not None and material.pk not in seen:
                    seen.add(material.pk)
                    materials.append({
                        "id": material.pk, "name": material.name,
                        "measure": (
                            "METER" if material.sells_by_metre
                            else "SQM" if material.is_roll_material else "PIECE"
                        ),
                    })
        already = self.get_queryset().filter(source_receipt=receipt).order_by("id")
        return Response({
            "receipt": str(receipt.pk),
            "order_number": receipt.order_number,
            "materials": materials,
            "leftovers": LeftoverSerializer(already, many=True).data,
        })
