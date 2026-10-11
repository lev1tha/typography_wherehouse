from decimal import Decimal

from datetime import date, datetime

from django.db import transaction
from django.db.models import Count, F, ProtectedError
from django.http import HttpResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import (
    IsAdmin,
    IsAdminOrAccountantRead,
    IsAdminOrReadOnly,
    IsNotAccountant,
    SeesMoney,
)
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
    SupplierOpeningDebt,
    SupplierPayment,
    Supply,
)
from .rolls import (
    InsufficientStock,
    SupplierPaymentError,
    has_lots,
    pay_lot_supplier,
    receive_lot,
    stocktake_roll,
    take_out,
    write_off_roll,
)
from .supplies import (
    SupplyError,
    duplicate_map,
    find_duplicate,
    move_supply_date,
    pay_supply,
    post_supply,
    supply_summary,
    sync_supply_payment,
    unpost_supply,
)
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
    SupplierOpeningDebtSerializer,
    SupplierPaymentSerializer,
    SupplierReturnSerializer,
    SupplierSerializer,
    StockTransferInputSerializer,
    StockTransferSerializer,
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


def _lock(day, what):
    """Замок периода по ДАТЕ ОПЕРАЦИИ (F4/PNL-01). Даты нет — операция идёт
    сегодняшним днём, и сегодня тоже может быть закрыто."""
    ensure_open(day or timezone.localdate(), what)


def _closed_movements(material) -> bool:
    """Есть ли у материала движения или партии в закрытом периоде."""
    from finance.periods import closed_through

    limit = closed_through()
    if not limit:
        return False
    from finance.periods import local_day

    days = [local_day(d) for d in material.inventory_logs.values_list("happened_at", flat=True)]
    days += [local_day(d) for d in material.rolls.values_list("received_at", flat=True)]
    return any(d is not None and d <= limit for d in days)


def _parse_day(raw):
    """Дата оплаты из запроса: пусто — None («сегодня»), кривая или будущая —
    False (этих денег ещё нет, а молча подставить сегодня — потерять дату)."""
    if raw in (None, ""):
        return None
    try:
        day = date.fromisoformat(str(raw))
    except ValueError:
        return False
    return False if day > timezone.localdate() else day


def _paid_account(data):
    """Счёт, с которого заплатили поставщику, или None.

    «В долг» и вовсе не указанный способ — одно и то же для кассы: движения
    денег не было. Разводить их отдельными значениями не нужно, а вот молча
    подставлять «наличные» нельзя — так в кассе появился бы расход, которого
    никто не делал. И для долга это одно и то же (D-195): счёта нет — приход
    встаёт долгом поставщику.
    """
    value = data.get("payment") or ""
    return value if value in ("CASH", "BANK") else None


def _intake_warnings(rows) -> list:
    """Предупреждения приёмки: лист не того размера, что в карточке (F7)."""
    from .lot_correction import sheet_size_warning

    out = []
    for material, form, width, height in rows:
        if form != "SHEET":
            continue
        warning = sheet_size_warning(material, width, height)
        if warning:
            out.append(warning)
    return out


class MaterialViewSet(viewsets.ModelViewSet):
    """Warehouse catalogue. Read for all staff; create/edit for admins.

    Supports ?search=<name|цвет|артикул>, ?ordering=name|quantity|price_per_unit
    and filters ?type=&color=&thickness_mm=, matching the warehouse screens.
    """

    # rolls — ради stock_value: он считается по остаткам партий, и без prefetch
    # каждый материал в списке уводил бы в отдельный запрос.
    queryset = Material.objects.prefetch_related("images", "rolls__placements", "price_tiers").all()
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

    def list(self, request, *args, **kwargs):
        """Список; `?export=csv` — каталог с остатками и ценами файлом для
        Excel (XL-06/STK-09), с теми же фильтрами и без страниц."""
        if request.query_params.get("export") == "csv":
            from .exports import catalog_csv, csv_response

            qs = self.filter_queryset(self.get_queryset()).select_related("type")
            money = bool(getattr(request.user, "sees_money", False))
            return csv_response(catalog_csv(qs, money=money), "katalog.csv")
        return super().list(request, *args, **kwargs)

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
        # Замок периода (F4/PNL-01): удаление насовсем уносит приходы и брак из
        # журнала — и потери, и закуп принятого месяца менялись задним числом.
        # Материал с движениями в закрытом периоде только прячем: история
        # остаётся, цифры закрытого месяца — тоже.
        locked_history = _closed_movements(material)
        if not locked_history and not material.transaction_items.exists():
            lots = material.rolls.count()
            logs = material.inventory_logs.count()
            try:
                with transaction.atomic():
                    # PROTECT на обеих связях — убираем их своими руками и в
                    # одной транзакции, чтобы при отказе не осталось материала
                    # без партий.
                    # Оплаты партий остаются в кассе со встречной записью
                    # (аудит Б-13): материала не было — деньги вернулись.
                    from finance import cash

                    for lot in material.rolls.all():
                        cash.reverse_supplier_payments(
                            roll=lot, note=f"Удалён материал «{name}»", user=request.user
                        )
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
        detail = (
            "Материал скрыт из каталога — по нему есть движения в закрытом "
            "периоде, и удалить их нельзя: поменялись бы цифры принятого месяца."
            if locked_history else
            "Материал скрыт из каталога — по нему были продажи, "
            "и удалить его нельзя: поехали бы суммы старых чеков. "
            "В расчёты нового периода он больше не входит."
        )
        return Response(
            {"archived": True, "period_closed": locked_history, "detail": detail},
            status=status.HTTP_200_OK,
        )

    def perform_update(self, serializer):
        """Правка карточки — и журнал «было → стало» по ценам (XL-07/CALC-09):
        из 14 денежных правок раньше записывалась одна."""
        from .reprice import log_price_changes, snapshot

        before = snapshot(serializer.instance)
        material = serializer.save()
        log_price_changes(self.request.user, material, before, snapshot(material))

    @action(detail=False, methods=["post"], url_path="reprice", permission_classes=[IsAdmin])
    def reprice(self, request):
        """POST /materials/reprice/ — переоценка «× %» (XL-05/CALC-09).

        Тело: percent (может быть минусом), step (округление: 0.01 / 1 / 10,
        по умолчанию 1 сом), fields (по умолчанию цены продажи), отбор: ids,
        type, thickness_mm, form (PIECE|SHEET|ROLL), search; apply — провести.
        Без apply — предпросмотр «было → стало», ничего не пишет.
        """
        from .numbers import normalize_number_text
        from .reprice import REPRICE_FIELDS, apply_changes, plan_json, plan_reprice

        data = request.data
        try:
            percent = Decimal(str(normalize_number_text(data.get("percent"))))
            step = Decimal(str(normalize_number_text(data.get("step") or "1")))
        except (ArithmeticError, ValueError, TypeError):
            return Response({"detail": "Укажите процент числом, например 10 или -5."},
                            status=status.HTTP_400_BAD_REQUEST)
        if percent == 0 or not (Decimal("-90") <= percent <= Decimal("500")):
            return Response({"detail": "Процент — от −90 до 500 и не ноль."},
                            status=status.HTTP_400_BAD_REQUEST)
        fields = [f for f in (data.get("fields") or REPRICE_FIELDS) if f in REPRICE_FIELDS + ("cut_rate_per_pm",)]
        if not fields:
            return Response({"detail": "Не выбрано, какие цены менять."}, status=status.HTTP_400_BAD_REQUEST)

        qs = Material.objects.filter(is_archived=False)
        ids = data.get("ids")
        if isinstance(ids, list) and ids:
            qs = qs.filter(pk__in=[i for i in ids if str(i).isdigit()])
        if data.get("type"):
            qs = qs.filter(type_id=data["type"])
        if data.get("thickness_mm") not in (None, ""):
            try:
                qs = qs.filter(thickness_mm=Decimal(str(normalize_number_text(data["thickness_mm"]))))
            except (ArithmeticError, ValueError):
                return Response({"detail": "Толщина — числом."}, status=status.HTTP_400_BAD_REQUEST)
        form = data.get("form")
        if form == "PIECE":
            qs = qs.filter(is_roll_material=False)
        elif form in (Material.IntakeForm.SHEET, Material.IntakeForm.ROLL):
            qs = qs.filter(is_roll_material=True, intake_form=form)
        if data.get("search"):
            qs = qs.filter(name__icontains=data["search"])
        plan = plan_reprice(qs.order_by("name"), percent, fields=fields, step=step)
        sign = "+" if percent > 0 else ""
        why = f"переоценка {sign}{percent.normalize():f} %"
        if data.get("apply") in (True, "true", "1", 1):
            count = apply_changes(plan, request.user, why=why)
            AuditLog.record(request.user, f"Прайс: {why}, материалов {count}", kind="price")
            return Response({"applied": count, "rows": plan_json(plan)})
        return Response({"applied": 0, "rows": plan_json(plan)})

    @action(detail=False, methods=["get"], url_path="on-date", permission_classes=[IsAuthenticated, SeesMoney])
    def on_date(self, request):
        """GET /materials/on-date/?date=ГГГГ-ММ-ДД — склад на конец дня (STK-04):
        из снимка, если он снят, иначе расчётом. `&export=csv` — файлом."""
        from .snapshots import stock_on_date

        raw = request.query_params.get("date")
        try:
            day = date.fromisoformat(raw) if raw else timezone.localdate()
        except ValueError:
            return Response({"detail": "Дата — в виде ГГГГ-ММ-ДД."}, status=status.HTTP_400_BAD_REQUEST)
        if day > timezone.localdate():
            return Response({"detail": "Склад будущего дня не известен."}, status=status.HTTP_400_BAD_REQUEST)
        data = stock_on_date(day)
        if request.query_params.get("export") == "csv":
            from .exports import csv_response, num, QTY

            def rows():
                yield ["Материал", "Остаток, кв.м (шт)", "Остаток", "Ед.", "По закупу"]
                for r in data["rows"]:
                    yield [r["name"], num(r["quantity"], QTY), num(r["units"]), r["unit_label"], num(r["value"])]
                yield ["Итого", "", "", "", num(data["value"])]

            return csv_response(rows(), f"sklad-{day:%Y-%m-%d}.csv")
        return Response(data)

    @action(detail=False, methods=["get"], url_path="reorder")
    def reorder(self, request):
        """GET /materials/reorder/ — «К заказу» (STK-06): материалы на пороге и
        ниже, сколько докупить до двух минимумов, у кого брали последний раз.
        `?export=csv` — то же файлом для Excel."""
        from .reorder import reorder_rows

        rows = reorder_rows()
        if not getattr(request.user, "sees_money", False):
            for row in rows:
                row["unit_cost"] = row["sum"] = None
        if request.query_params.get("export") == "csv":
            from .exports import csv_response, reorder_csv

            return csv_response(reorder_csv(rows), "k-zakazu.csv")
        return Response({"results": rows, "count": len(rows)})

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

        ``mode="upsert"`` (XL-05, волна 2) — существующее название не ошибка, а
        ОБНОВЛЕНИЕ: у него меняются цены, ставка резки, опт и минимальный
        остаток из непустых ячеек строки; новые названия заводятся. С
        ``preview=true`` ничего не пишет и отвечает «было → стало».

        ``create_sites=[«Лазер»]`` (RU-N11) — завести эти производства, если их
        нет в справочнике; без него неизвестное производство — 400 с
        ``missing_sites``.
        """
        rows = request.data.get("rows")
        if not isinstance(rows, list) or not rows:
            return Response(
                {"detail": "Нет строк для сохранения."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        upsert = request.data.get("mode") == "upsert"
        preview = request.data.get("preview") in (True, "true", "1", 1)
        # Всё или ничего — и вместе со справочником, заведённым по ходу (RU-N11):
        # ошибка в любой строке или предпросмотр откатывают и новое производство.
        with transaction.atomic():
            response = self._bulk(request, rows, upsert, preview)
            if response.status_code >= 400 or preview:
                transaction.set_rollback(True)
        return response

    def _bulk(self, request, rows, upsert, preview):
        from .reprice import UPSERT_FIELDS, apply_changes, plan_json

        # Справочники и занятые названия — одним запросом на всю пачку, а не на
        # каждую строку. Регистр сводится в Python: в SQLite `iexact` не
        # складывает кириллицу, и «форекс» разошёлся бы с «Форекс» на деве.
        context = {
            "types": build_ref_index(MaterialType.objects.all()),
            "sites": build_ref_index(ProductionSite.objects.all()),
        }
        # Неизвестное производство (RU-N11, перепроверка 10.10): одна ячейка
        # «Лазер» отклоняла всю пачку, и надо было идти в справочник, заводить
        # его и вставлять заново. Теперь сервер называет, чего нет
        # (`missing_sites`), сетка предлагает «создать для всех строк», и
        # повтор с `create_sites` заводит справочник в той же транзакции.
        missing = {}
        for row in rows:
            text = str((row or {}).get("production") or "").strip() if isinstance(row, dict) else ""
            if text and text.casefold() not in context["sites"]:
                missing.setdefault(text.casefold(), text)
        confirmed = {str(n).strip().casefold() for n in (request.data.get("create_sites") or []) if str(n).strip()}
        created_sites = []
        for key, name in missing.items():
            if key in confirmed:
                site = ProductionSite.objects.create(code=ProductionSite.make_code(name), name=name[:80])
                context["sites"][key] = site
                context["sites"][str(site.pk)] = site
                created_sites.append(site.name)
        missing_sites = [name for key, name in missing.items() if key not in confirmed]
        taken = {m.name.strip().casefold(): m for m in Material.objects.all()}

        cleaned, updates, unchanged, errors, seen = [], [], 0, [], set()
        for index, row in enumerate(rows):
            serializer = MaterialBulkRowSerializer(data=row, context=context)
            if not serializer.is_valid():
                errors.append({"row": index, "fields": serializer.errors})
                continue
            data = serializer.validated_data
            key = data["name"].strip().casefold()
            # Две разные беды с одинаковым исходом, но разными объяснениями:
            # такое название уже в каталоге — или строка задублирована в пачке.
            if key in seen:
                message = f"«{data['name']}» повторяется в списке."
            elif key in taken and upsert:
                seen.add(key)
                material = taken[key]
                given = {f for f in UPSERT_FIELDS if row.get(f) not in (None, "") and f in data}
                # Колонка «за лист/шт» одна: у листа это цена листа, у
                # штучного — цена единицы. Сетка решает по размеру листа в
                # строке, а при обновлении размера в строке может не быть.
                if material.is_roll_material and "price_per_unit" in given and "piece_price" not in given:
                    data["piece_price"] = data["price_per_unit"]
                    given = (given - {"price_per_unit"}) | {"piece_price"}
                if not material.is_roll_material and "piece_price" in given and "price_per_unit" not in given:
                    data["price_per_unit"] = data["piece_price"]
                    given = (given - {"piece_price"}) | {"price_per_unit"}
                changes = [
                    (f, getattr(material, f), data[f]) for f in UPSERT_FIELDS
                    if f in given and getattr(material, f) != data[f]
                ]
                if changes:
                    updates.append({"material": material, "changes": changes})
                else:
                    unchanged += 1
                continue
            elif key in taken:
                message = f"«{data['name']}» уже есть в каталоге."
            else:
                seen.add(key)
                cleaned.append(data)
                continue
            errors.append({"row": index, "fields": {"name": [message]}})

        if errors:
            body = {"errors": errors}
            if missing_sites:
                body["missing_sites"] = missing_sites
            return Response(body, status=status.HTTP_400_BAD_REQUEST)

        if preview:
            return Response({
                "preview": True,
                "create": [d["name"] for d in cleaned],
                "update": plan_json(updates),
                "unchanged": unchanged,
                "create_sites": created_sites,
            })

        created = [Material.objects.create(**data) for data in cleaned]
        updated = apply_changes(updates, request.user, why="обновление пачкой") if updates else 0
        for name in created_sites:
            AuditLog.record(request.user, f"Каталог: из «Ввести пачкой» заведено производство «{name}»",
                            kind="stock")
        if created:
            AuditLog.record(request.user, f"Каталог пополнен пачкой: {len(created)} материалов")
        if updated:
            AuditLog.record(request.user, f"Прайс обновлён пачкой: {updated} материалов", kind="price")
        return Response(
            {
                "created": len(created),
                "updated": updated,
                "unchanged": unchanged,
                "created_sites": created_sites,
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
            paid_account=_paid_account(data),
            # Приход без указанной оплаты — долг (D-195): «в долг» и пусто — одно.
            on_credit=_paid_account(data) is None,
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
        _lock(data.get("happened_on"), "Провести инвентаризацию этой датой")
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
        # Дата пересчёта (G3-N3): недостача и излишек ложатся в месяц, когда
        # считали, а не когда внесли. Замок периода проверен выше.
        from .waste import _moment

        day = data.get("happened_on")
        moment = _moment(day)
        if day:
            reason = f"{reason} (пересчёт на {day:%d.%m.%Y})"
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
                    happened_at=moment,
                )
            elif delta > 0:
                # Излишек — со стоимостью по партии, куда лёг (F5/PNL-04), и в
                # партию посвежее: недостача уходила FIFO со старейшей
                # непустой, «ошибся — поправил» возвращает туда же.
                restore_area(
                    material, delta, user=request.user,
                    reason=reason, log_type=InventoryLog.Type.ADJUSTMENT,
                    happened_at=moment, newest_first=True,
                )
            material.refresh_from_db()
        elif delta < 0 and has_lots(material):
            # Недостача у штучного с партиями — из партий, по их ценам. Одним
            # числом остатка партия продолжала бы числить пропавшие штуки, и
            # следующая продажа брала бы себестоимость из партии, которой нет.
            take_out(
                material, -delta, user=request.user,
                reason=reason, log_type=InventoryLog.Type.ADJUSTMENT,
                happened_at=moment,
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
                happened_at=moment,
                # Недостача — это ДЕНЬГИ, и цифру нужно сохранить здесь: у
                # правки остатка нет ни строки чека, ни партии, и восстановить
                # её потом не по чему. У площадных материалов её считает
                # `consume_area` по партиям; у штучных цена одна — закупочная
                # из карточки (так же оценивает остаток `stock_value`).
                # До 19.09 её не писали вовсе: 4 000 диодов ушли правкой на
                # 14 000 сом, и в отчётах эти деньги просто исчезли.
                #
                # Излишек — тоже деньги, по той же цене (F5/PNL-04): ОПиУ гасит
                # им недостачу, иначе «ошибся — поправил» оставлял убыток.
                cost=(
                    (abs(delta) * (material.purchase_price or Decimal("0"))).quantize(Decimal("0.01"))
                    if delta
                    else None
                ),
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

        _lock(None, "Свести остаток сегодняшним днём")
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
            site=data.get("site"),
            user=request.user,
            declared_length=data.get("declared_length"),
            paid_account=_paid_account(data),
            # Приход без указанной оплаты — долг (D-195): «в долг» и пусто — одно.
            on_credit=_paid_account(data) is None,
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
        body = dict(MaterialSerializer(roll.material, context={"request": request}).data)
        # Размер листа не совпал с карточкой (аудит F7) — не запрет, а
        # предупреждение в момент приёмки: лист продаётся по площади карточки.
        body["warnings"] = _intake_warnings([(roll.material, roll.form, roll.width, roll.height)])
        return Response(body, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="write-off", permission_classes=[IsAdmin])
    def write_off(self, request):
        """POST /materials/write-off/ — write off stock (damage/defect/loss)."""
        serializer = WriteOffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        _lock(data.get("happened_on"), "Списать этой датой")
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
        # Брак — это деньги: себестоимость уходит в журнал (`InventoryLog.cost`)
        # и оттуда в прибыль. Штучный с партиями списывается ИЗ ПАРТИИ, а не
        # одним числом остатка: иначе партия числила бы выброшенную штуку, а
        # себестоимость не писалась — брак пропадал со склада мимо всех цифр.
        from .waste import _moment

        take_out(
            material, Decimal(data["quantity"]), user=request.user,
            reason=reason, log_type=InventoryLog.Type.WRITE_OFF,
            happened_at=_moment(data.get("happened_on")),
        )
        material.refresh_from_db()
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

    def list(self, request, *args, **kwargs):
        """`?export=csv` — журнал движений файлом для Excel (те же фильтры)."""
        if request.query_params.get("export") == "csv":
            from .exports import csv_response, journal_csv

            qs = self.filter_queryset(self.get_queryset())
            money = bool(getattr(request.user, "sees_money", False))
            return csv_response(journal_csv(qs, money=money), "zhurnal-sklada.csv")
        return super().list(request, *args, **kwargs)


class RollViewSet(viewsets.ReadOnlyModelViewSet):
    """Rolls (lots) of roll-materials — list & filter by material."""

    queryset = Roll.objects.select_related("material", "supply_line__supply", "site").prefetch_related(
        "placements").all()
    serializer_class = RollSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ["material", "production"]
    ordering = ["received_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        # ?site=<id> — партии, у которых хоть что-то лежит на этой площадке
        # (своя площадка или перевезённая часть); ?site=none — без площадки.
        site = self.request.query_params.get("site")
        if site == "none":
            qs = qs.filter(site__isnull=True)
        elif site and site.isdigit():
            from django.db.models import Q

            qs = qs.filter(Q(site_id=site) | Q(placements__site_id=site, placements__area__gt=0)).distinct()
        return qs

    def list(self, request, *args, **kwargs):
        """`?export=csv` — партии файлом для Excel (те же фильтры)."""
        if request.query_params.get("export") == "csv":
            from .exports import csv_response, lots_csv

            qs = self.filter_queryset(self.get_queryset()).select_related("production")
            money = bool(getattr(request.user, "sees_money", False))
            return csv_response(lots_csv(qs, money=money), "partii.csv")
        return super().list(request, *args, **kwargs)

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
        _lock(None, "Промерить рулон сегодняшним днём")
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
        _lock(data.get("happened_on"), "Списать этой датой")
        reason = serializer.reason_text()
        from .waste import _moment

        try:
            area = write_off_roll(
                roll, data["metres"], reason=reason, user=request.user,
                happened_at=_moment(data.get("happened_on")),
            )
        except InsufficientStock as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        label = roll.code or f"№{roll.pk}"
        # Себестоимость — по цене этого рулона, закуп × площадь / принятая
        # (STK-10), та же цифра, что в журнале склада.
        cost = roll.cost_of(area).quantize(Decimal("0.01"))
        AuditLog.record(
            request.user,
            f"{reason} Рулон {label} «{roll.material.name}»: {data['metres']} м "
            f"({area} кв.м, себестоимость {cost} сом); "
            f"в рулоне осталось {roll.metres_remaining} м",
        )
        return Response(RollSerializer(roll).data)


    @action(detail=True, methods=["post"], url_path="pay-supplier", permission_classes=[IsAdmin])
    def pay_supplier(self, request, pk=None):
        """POST /rolls/<id>/pay-supplier/ {amount?, account, paid_on?} — заплатить
        поставщику за партию, взятую в долг. Пустая сумма — весь долг."""
        roll = self.get_object()
        paid_on = _parse_day(request.data.get("paid_on"))
        if paid_on is False:
            return Response({"detail": "Некорректная дата оплаты."}, status=status.HTTP_400_BAD_REQUEST)
        ensure_open(paid_on or timezone.localdate(), "Провести оплату этой датой")
        raw = request.data.get("amount")
        try:
            # Пустая сумма — весь долг партии; долг расчётом (D-195), в том числе
            # у старого прихода без оплаты и без отметки «в долг».
            from .supplier_debts import lot_debt

            amount = max(lot_debt(roll), Decimal("0")) if raw in (None, "") else Decimal(str(raw))
            left = pay_lot_supplier(
                roll, amount, request.data.get("account"), paid_on=paid_on, user=request.user,
            )
        except (SupplierPaymentError, ArithmeticError, ValueError) as e:
            text = str(e) if isinstance(e, SupplierPaymentError) else "Некорректная сумма."
            return Response({"detail": text}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Оплата поставщику за партию «{roll.material.name}» {roll.dimensions_label}: "
            f"{amount} сом, долг остался {left}",
        )
        roll.refresh_from_db()
        return Response(RollSerializer(roll, context={"request": request}).data)


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
        old = MaterialMonthOpening.objects.filter(
            material=data["material"], year=data["year"], month=data["month"],
        ).values_list("quantity", flat=True).first()
        row, _created = MaterialMonthOpening.objects.update_or_create(
            material=data["material"], year=data["year"], month=data["month"],
            defaults={"quantity": data["quantity"], "updated_by": request.user},
        )
        self._journal(request.user, row, old, row.quantity)
        return Response(self.get_serializer(row).data, status=status.HTTP_200_OK)

    def perform_update(self, serializer):
        old = serializer.instance.quantity
        row = serializer.save(updated_by=self.request.user)
        self._journal(self.request.user, row, old, row.quantity)

    def perform_destroy(self, instance):
        self._journal(self.request.user, instance, instance.quantity, None)
        instance.delete()

    @staticmethod
    def _journal(user, row, old, new):
        """Журнал действий «было → стало» (XL-07, перепроверка 10.10): остаток
        на начало месяца — ручная цифра складского листа, и её правка меняла
        конец месяца молча, без следа, кто и когда."""
        if old is not None and new is not None and Decimal(old) == Decimal(new):
            return

        def n(value):
            if value is None:
                return "—"
            return format(Decimal(value).normalize(), "f").replace(".", ",")

        AuditLog.record(
            user,
            f"Склад: остаток на начало {row.month:02d}.{row.year} «{row.material.name}»: {n(old)} → {n(new)}",
            kind="stock",
        )


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


def _truthy(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def _supply_error(exc):
    """Ошибка денег поставщика → ответ. Курс оплаты далеко от курса накладной
    (RU-N18) — 409 с подтверждением, как предупреждения кассы; остальное — 400."""
    from .supplier_ledger import RateNeedsConfirmation

    if isinstance(exc, RateNeedsConfirmation):
        return Response(
            {"detail": str(exc), "needs_confirmation": True, "code": exc.code},
            status=status.HTTP_409_CONFLICT,
        )
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _day_or_400(raw, what="Дата"):
    """Дата из запроса → (date | None, ошибка | None)."""
    if raw in (None, ""):
        return None, None
    try:
        return date.fromisoformat(str(raw)[:10]), None
    except ValueError:
        return None, Response({"detail": f"{what}: некорректная дата."}, status=status.HTTP_400_BAD_REQUEST)


def _csv_response(text: str, filename: str):
    # BOM — чтобы русский Excel открыл кириллицу без «мастера импорта».
    response = HttpResponse("﻿" + text, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _changes_text(pairs) -> str:
    """«поле: было → стало; …» для журнала действий (XL-07/F3)."""
    return "; ".join(f"{name}: {old if old not in (None, '') else '—'} → {new if new not in (None, '') else '—'}"
                     for name, old, new in pairs if old != new)


class SupplierViewSet(viewsets.ModelViewSet):
    """Справочник поставщиков.

    Заводит его тот, кто принимает товар: новая фирма всплывает в момент
    приёмки, и гонять складовщика к админу за строчкой справочника — верный
    способ получить накладную «без поставщика». Удалять — только админ.
    Карточка поставщика (`statement`) и деньги в списке — админу и бухгалтеру.
    """

    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated, IsNotAccountant]
    pagination_class = None
    search_fields = ["name", "phone", "inn"]
    ordering = ["name"]

    def get_queryset(self):
        qs = Supplier.objects.prefetch_related(
            "supplies__lines", "supplies__payments", "supplies__returns", "payments__offsets",
            "opening_debts",
        )
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
        if (
            supplier.supplies.exists() or supplier.payments.exists()
            or supplier.opening_debts.exists()
        ):
            supplier.is_archived = True
            supplier.save(update_fields=["is_archived"])
            return Response(
                {"archived": True, "detail": "Поставщик скрыт — по нему есть накладные и расчёты."},
                status=status.HTTP_200_OK,
            )
        supplier.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], permission_classes=[IsAuthenticated, SeesMoney])
    def statement(self, request, pk=None):
        """GET /suppliers/<id>/statement/?date_from&date_to[&export=csv] —
        карточка поставщика: накладные, платежи, возвраты, сальдо."""
        from .supplier_ledger import statement, statement_csv, supplier_balance

        supplier = self.get_object()
        d_from, err = _day_or_400(request.query_params.get("date_from"), "Начало периода")
        if err:
            return err
        d_to, err = _day_or_400(request.query_params.get("date_to"), "Конец периода")
        if err:
            return err
        data = statement(supplier, d_from, d_to)
        if request.query_params.get("export") == "csv":
            return _csv_response(statement_csv(data), f"supplier-{supplier.id}-statement.csv")
        data["balance"] = supplier_balance(supplier)
        data["advances"] = SupplierPaymentSerializer(
            [p for p in supplier.payments.all() if p.is_advance], many=True,
            context=self.get_serializer_context(),
        ).data
        data["opening_debts"] = SupplierOpeningDebtSerializer(
            supplier.opening_debts.all(), many=True,
        ).data
        return Response(data)


class SupplierOpeningDebtViewSet(viewsets.GenericViewSet, mixins.ListModelMixin,
                                 mixins.CreateModelMixin, mixins.DestroyModelMixin):
    """Начальный долг поставщику — входящее сальдо переезда (без партий).
    Входит в сальдо поставщика, но не в закуп периода и не в кассу."""

    serializer_class = SupplierOpeningDebtSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None
    queryset = SupplierOpeningDebt.objects.select_related("supplier")
    filterset_fields = ["supplier"]

    def create(self, request, *args, **kwargs):
        from .supplier_ledger import add_opening_debt

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        try:
            row = add_opening_debt(
                d["supplier"], d["amount"], as_of=d.get("as_of"), note=d.get("note", ""),
                user=request.user,
            )
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Начальный долг поставщику «{row.supplier.name}»: {row.amount} сом на {row.as_of:%d.%m.%Y}"
            + (f" ({row.note})" if row.note else ""),
        )
        return Response(self.get_serializer(row).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        AuditLog.record(
            self.request.user,
            f"Начальный долг поставщику «{instance.supplier.name}» удалён: {instance.amount} сом "
            f"на {instance.as_of:%d.%m.%Y}",
        )
        instance.delete()


class SupplierPaymentViewSet(viewsets.ModelViewSet):
    """Платежи поставщикам — отдельные строки (cash-06, cash-07).

    GET — админу и бухгалтеру; создать (оплата по накладной или аванс),
    поправить счёт/дату/примечание, удалить и зачесть аванс в накладную —
    только администратор. Каждое действие — в журнале «было → стало».
    """

    serializer_class = SupplierPaymentSerializer
    permission_classes = [IsAdminOrAccountantRead]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = SupplierPayment.objects.select_related("supplier", "supply", "created_by").prefetch_related("offsets")
        q = self.request.query_params
        if q.get("supplier"):
            qs = qs.filter(supplier_id=q["supplier"])
        if q.get("supply"):
            qs = qs.filter(supply_id=q["supply"])
        if q.get("advance") == "1":
            qs = qs.filter(kind=SupplierPayment.Kind.PAYMENT, supply__isnull=True)
        if q.get("kind"):
            qs = qs.filter(kind=q["kind"])
        if q.get("date_from"):
            qs = qs.filter(paid_on__gte=q["date_from"])
        if q.get("date_to"):
            qs = qs.filter(paid_on__lte=q["date_to"])
        return qs

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        if request.query_params.get("export") == "csv":
            from .supplier_ledger import payments_csv

            return _csv_response(payments_csv(qs), "supplier-payments.csv")
        return Response(self.get_serializer(qs, many=True).data)

    def create(self, request, *args, **kwargs):
        from .supplier_ledger import record_payment

        data = request.data
        paid_on = _parse_day(data.get("paid_on"))
        if paid_on is False:
            return Response({"detail": "Некорректная дата оплаты."}, status=status.HTTP_400_BAD_REQUEST)
        supply = supplier = None
        if data.get("supply") not in (None, ""):
            supply = get_object_or_404(Supply, pk=data["supply"])
        elif data.get("supplier") not in (None, ""):
            supplier = get_object_or_404(Supplier, pk=data["supplier"])
        try:
            payment = record_payment(
                supply=supply, supplier=supplier, amount=data.get("amount"),
                account=data.get("account"), paid_on=paid_on, rate=data.get("rate"),
                note=str(data.get("note") or "")[:255], user=request.user,
                confirm_rate=_truthy(data.get("confirm_rate")),
            )
        except SupplyError as exc:
            return _supply_error(exc)
        AuditLog.record(request.user, "Платёж поставщику: " + _payment_text(payment))
        payment = self.get_queryset().get(pk=payment.pk)
        return Response(self.get_serializer(payment).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        from .supplier_ledger import update_payment

        payment = self.get_object()
        paid_on = _parse_day(request.data.get("paid_on"))
        if paid_on is False:
            return Response({"detail": "Некорректная дата."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            changes = update_payment(
                payment, account=request.data.get("account"), paid_on=paid_on,
                note=request.data.get("note"), user=request.user,
            )
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if changes:
            AuditLog.record(
                request.user,
                f"Платёж поставщику изменён ({_payment_text(payment)}): " + "; ".join(changes),
            )
        return Response(self.get_serializer(self.get_queryset().get(pk=payment.pk)).data)

    def destroy(self, request, *args, **kwargs):
        from .supplier_ledger import delete_payment

        payment = self.get_object()
        try:
            text = delete_payment(payment, user=request.user)
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(request.user, f"Платёж поставщику удалён: {text}")
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def apply(self, request, pk=None):
        """POST /supplier-payments/<id>/apply/ {supply, amount} — зачесть аванс в накладную."""
        from .supplier_ledger import apply_advance

        advance = self.get_object()
        supply = get_object_or_404(Supply, pk=request.data.get("supply"))
        try:
            row = apply_advance(advance, supply, request.data.get("amount"), user=request.user)
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Зачёт аванса поставщику от {advance.paid_on:%d.%m.%Y}: {row.amount} сом в накладную "
            f"{supply.number or f'#{supply.pk}'}",
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=row.pk)).data)


def _payment_text(p: SupplierPayment) -> str:
    where = f"накладная {p.supply.number or f'#{p.supply_id}'}" if p.supply_id else "аванс"
    who = p.supplier.name if p.supplier_id else "без поставщика"
    cur = f" ({p.amount_fc} {p.currency} по {p.rate})" if p.amount_fc is not None else ""
    return f"{p.get_kind_display().lower()} {p.amount} сом{cur}, {p.get_account_display() or '—'}, {p.paid_on:%d.%m.%Y}, {who}, {where}"


class SupplyViewSet(viewsets.ModelViewSet):
    """Приходные накладные — поставка целиком, одним документом.

    Создаёт и видит их тот, кто принимает товар (админ и складовщик); отменяет
    только админ: отмена снимает материал с остатка. Закупочные цены, суммы,
    долг и платежи складовщику приходят пустыми (STAFF-07).
    """

    serializer_class = SupplySerializer
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filterset_fields = ["supplier"]
    search_fields = ["number", "note", "supplier__name"]
    ordering = ["-received_on", "-id"]

    def get_queryset(self):
        qs = (
            Supply.objects.select_related("supplier", "created_by")
            .prefetch_related(
                "lines__material", "lines__roll", "payments__supplier", "payments__supply",
                "payments__offsets",
                "returns__lines",
            )
        )
        d_from = self.request.query_params.get("date_from")
        d_to = self.request.query_params.get("date_to")
        if d_from:
            qs = qs.filter(received_on__gte=d_from)
        if d_to:
            qs = qs.filter(received_on__lte=d_to)
        if self.request.query_params.get("unpaid") == "1":
            # Долг считается из строк, старого поля и платежей — одной формулой
            # (`Supply.debt`), поэтому отбор в Python по уже загруженным связям.
            ids = [s.id for s in qs if s.debt > 0]
            qs = qs.filter(id__in=ids)
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        if getattr(self, "action", None) == "list":
            ctx["dupes"] = duplicate_map()
        return ctx

    def list(self, request, *args, **kwargs):
        """`?export=csv` — список накладных файлом для Excel (те же фильтры,
        поиск и сортировка, все страницы; XL-06)."""
        if request.query_params.get("export") == "csv":
            from .exports import csv_response, supplies_csv

            qs = self.filter_queryset(self.get_queryset())
            money = bool(getattr(request.user, "sees_money", False))
            return csv_response(supplies_csv(qs, money=money, dupes=duplicate_map()), "nakladnye.csv")
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Оплата поставщику — деньги из кассы, и проводит её администратор (как в
        # `update` и `pay`). Накладную складовщик заводит без оплаты; приход с
        # полями оплаты в теле раньше писал расход в кассу в обход этого права.
        if not request.user.is_admin_role:
            data = serializer.validated_data
            if (data.get("paid_amount") or 0) > 0 or data.get("paid_account"):
                return Response(
                    {"detail": "Оплату поставщику проводит администратор."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        # Приход двигает закуп месяца — в закрытый период его не заводим.
        ensure_open(serializer.validated_data.get("received_on"), "Провести накладную этой датой")
        lines = serializer.validated_data.pop("lines", [])
        # Тот же ввод дважды (F11, G3-N2): 409 с ссылкой на уже введённую; с
        # подтверждением (`force`) — проходит и пишется в журнал.
        d = serializer.validated_data
        total = sum((Decimal(str(line.get("cost") or 0)) for line in lines), Decimal("0"))
        duplicate = find_duplicate(
            supplier_id=d["supplier"].pk if d.get("supplier") else None,
            number=d.get("number"), received_on=d.get("received_on") or timezone.localdate(),
            total=total,
        )
        forced = duplicate is not None and (
            _truthy(request.data.get("force")) or _truthy(request.query_params.get("force"))
        )
        if duplicate is not None and not forced:
            return Response(
                {
                    "detail": (
                        f"Похоже, эта накладная уже введена: №{duplicate.number or duplicate.pk} от "
                        f"{duplicate.received_on:%d.%m.%Y}"
                        f"{f' ({duplicate.supplier.name})' if duplicate.supplier_id else ''}. "
                        "Если это другая поставка — подтвердите, и она будет проведена."
                    ),
                    "code": "duplicate_supply",
                    "duplicate": {
                        "id": duplicate.pk, "number": duplicate.number,
                        "received_on": duplicate.received_on,
                        "supplier_name": duplicate.supplier.name if duplicate.supplier_id else "",
                        "total_cost": duplicate.total_cost if request.user.sees_money else None,
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )
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
            f"{'Начальные остатки' if supply.is_opening else 'Приход'} по накладной "
            f"{supply.number or f'#{supply.pk}'}"
            f"{f' от {supply.supplier.name}' if supply.supplier_id else ''}: "
            f"{supply.lines.count()} поз. на {supply.total_cost} сом"
            + (f" ({supply.total_foreign} {supply.currency} по {supply.rate})" if supply.is_foreign else ""),
        )
        if forced:
            AuditLog.record(
                request.user,
                f"Накладная {supply.number or f'#{supply.pk}'} проведена повторно, несмотря на "
                f"похожую №{duplicate.number or duplicate.pk} от {duplicate.received_on:%d.%m.%Y} "
                "(подтверждено пользователем)",
            )
        body = dict(self.get_serializer(supply).data)
        body["warnings"] = _intake_warnings([
            (line.material, line.form, line.width, line.height) for line in supply.lines.all()
        ])
        return Response(body, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        """Состав накладной не правим — только её «бумажную» часть.

        Переписывать проведённые строки значило бы двигать склад задним числом:
        часть материала уже могла уйти в заказы. Опечатка в цене или количестве —
        «Исправить приход», товар назад поставщику — «Вернуть поставщику»,
        ошибка иная — отмените накладную и заведите заново; номер, сумма по
        бумаге и оплата правятся спокойно. ДАТА переносится вместе с партиями и
        движениями склада (`move_supply_date`) и держится замком периода с обеих
        сторон: раньше уезжал только закуп, а партии и журнал оставались в
        старом месяце. Каждая правка пишется в журнал «было → стало».
        """
        if "lines" in request.data:
            return Response(
                {"detail": (
                    "Состав накладной не правится. Опечатку в цене или количестве исправьте "
                    "кнопкой «Исправить приход», товар назад поставщику — «Вернуть "
                    "поставщику»; ошиблись иначе — отмените накладную и заведите заново."
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
        old_paid, old_account = supply.paid_amount, supply.paid_account
        before = [
            ("номер", supply.number, None), ("сумма по бумаге", supply.stated_total, None),
            ("примечание", supply.note, None),
            ("поставщик", supply.supplier.name if supply.supplier_id else "", None),
            ("оплачено (поле накладной)", supply.paid_amount, None),
            ("счёт оплаты", supply.paid_account, None),
        ]
        pays = "paid_amount" in request.data or "paid_account" in request.data
        if pays and not request.user.is_admin_role:
            return Response(
                {"detail": "Оплату поставщику проводит администратор."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if pays:
            ensure_open(timezone.localdate(), "Провести оплату сегодняшним днём")
        with transaction.atomic():
            response = super().update(request, *args, **kwargs)
            if pays and response.status_code == 200:
                supply.refresh_from_db()
                # Оплата, изменённая правкой, обязана дойти до кассы — иначе
                # долг становится нулём, а деньги уходят из ящика без строки
                # в книге (аудит 26.09: 48 000 мимо кассы).
                try:
                    sync_supply_payment(
                        supply, old_amount=old_paid, old_account=old_account,
                        user=request.user,
                    )
                except SupplyError as e:
                    transaction.set_rollback(True)
                    return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if response.status_code == 200:
            fresh = Supply.objects.select_related("supplier").get(pk=supply.pk)
            after = [
                fresh.number, fresh.stated_total, fresh.note,
                fresh.supplier.name if fresh.supplier_id else "", fresh.paid_amount,
                fresh.paid_account,
            ]
            diff = _changes_text([(n, old, new) for (n, old, _), new in zip(before, after)])
            if diff:
                AuditLog.record(
                    request.user,
                    f"Накладная {fresh.number or f'#{fresh.pk}'} изменена: {diff}",
                )
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

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def pay(self, request, pk=None):
        """POST /supplies/<id>/pay/ {amount?, account, paid_on?, rate?, note?} —
        заплатить поставщику по накладной. Пустая сумма — весь долг. Накладная
        в валюте: сумма в её валюте, `rate` — курс на день оплаты."""
        supply = self.get_object()
        paid_on = _parse_day(request.data.get("paid_on"))
        if paid_on is False:
            return Response({"detail": "Некорректная дата оплаты."}, status=status.HTTP_400_BAD_REQUEST)
        ensure_open(paid_on or timezone.localdate(), "Провести оплату этой датой")
        raw = request.data.get("amount")
        try:
            if raw in (None, ""):
                amount = supply.debt_foreign if supply.is_foreign else supply.debt
            else:
                amount = Decimal(str(raw))
            left = pay_supply(
                supply, amount, request.data.get("account"), paid_on=paid_on, user=request.user,
                rate=request.data.get("rate"), note=str(request.data.get("note") or "")[:255],
                confirm_rate=_truthy(request.data.get("confirm_rate")),
            )
        except SupplyError as e:
            return _supply_error(e)
        except (ArithmeticError, ValueError):
            return Response({"detail": "Некорректная сумма."}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(
            request.user,
            f"Оплата по накладной {supply.number or f'#{supply.pk}'}: {amount} {supply.currency}, "
            f"долг остался {left} сом",
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=supply.pk)).data)

    @action(detail=True, methods=["post"], url_path="return", permission_classes=[IsAdmin])
    def return_goods(self, request, pk=None):
        """POST /supplies/<id>/return/ {lines: [{line, quantity}], returned_on?,
        mode: CREDIT|REFUND, account?, refund_amount?, note?} — вернуть товар
        поставщику (G1-N2)."""
        from .supplier_returns import return_to_supplier

        supply = self.get_object()
        returned_on = _parse_day(request.data.get("returned_on"))
        if returned_on is False:
            return Response({"detail": "Некорректная дата возврата."}, status=status.HTTP_400_BAD_REQUEST)
        label = supply.number or f"#{supply.pk}"
        before = (supply.total_cost, supply.paid_total, supply.debt)
        try:
            ret = return_to_supplier(
                supply, request.data.get("lines") or [], returned_on=returned_on,
                mode=request.data.get("mode") or "CREDIT", account=request.data.get("account") or "",
                refund_amount=request.data.get("refund_amount"),
                note=str(request.data.get("note") or "")[:255], user=request.user,
            )
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        fresh = self.get_queryset().get(pk=supply.pk)
        what = "; ".join(l.label for l in ret.lines.all())
        head = (
            f"Сумма накладной {before[0]} → {fresh.total_cost}" if ret.in_place else
            f"Накладная не изменена (как в бумаге), закуп {ret.returned_on:%d.%m.%Y} −{ret.amount} сом"
        )
        AuditLog.record(
            request.user,
            f"Возврат поставщику по накладной {label}: {what}. {head}, долг {before[2]} → {fresh.debt}"
            + (f"; деньги вернулись на {ret.refund_account}: {ret.refund} сом" if ret.refund else
               (f"; кредит у поставщика {fresh.overpaid} сом" if fresh.overpaid else "")),
        )
        body = dict(self.get_serializer(fresh).data)
        body["return"] = SupplierReturnSerializer(ret).data
        return Response(body)

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
            unpost_supply(supply, user=request.user)
        except SupplyError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        AuditLog.record(request.user, f"Отменена приходная накладная {summary}")
        return Response(status=status.HTTP_204_NO_CONTENT)


class StockTransferViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """Перемещения между площадками (STK-05/G4-N4).

    GET — список (?material=); POST — переместить: остаток и деньги не
    меняются, только где лежит. Пишет и складовщик — он и возит; бухгалтеру
    закрыто, как всё, что двигает склад.
    """

    serializer_class = StockTransferSerializer
    permission_classes = [IsAuthenticated, IsNotAccountant]
    filterset_fields = ["material", "from_site", "to_site"]

    def get_queryset(self):
        from .models import StockTransfer

        return StockTransfer.objects.select_related("material", "from_site", "to_site", "created_by")

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        from .sites import TransferError, transfer

        serializer = StockTransferInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        _lock(data.get("happened_on"), "Провести перемещение этой датой")
        try:
            doc = transfer(
                data["material"], data["quantity"], from_site=data.get("from_site"),
                to_site=data.get("to_site"), roll=data.get("roll"),
                happened_on=data.get("happened_on"), note=data.get("note", ""), user=request.user,
            )
        except TransferError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        frm = doc.from_site.name if doc.from_site_id else "без площадки"
        to = doc.to_site.name if doc.to_site_id else "без площадки"
        AuditLog.record(
            request.user,
            f"Перемещение «{doc.material.name}»: {doc.area.normalize():f} — {frm} → {to}"
            + (f" ({doc.note})" if doc.note else ""),
            kind="stock",
        )
        return Response(StockTransferSerializer(doc, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


class WasteView(APIView):
    """POST /warehouse/waste/ — отход (брак): списать несколько строк разом,
    теми же мерками, что и приход (лист ширина × высота × штук, площадь, метры
    рулона, количество).

    Пишет и складовщик: брак видит тот, кто стоит у станка и принимает
    товар, — экран отходов стоит рядом с приёмкой. Бухгалтеру, как и всему,
    что двигает склад, закрыто. Замок периода — по дате отхода (с 10.10,
    волна 2): прежнее «отход двигает только текущий остаток» было неверно —
    потери ложатся в месяц `happened_on`.
    """

    permission_classes = [IsAuthenticated, IsNotAccountant]

    def post(self, request):
        serializer = WasteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # Замок периода по дате отхода (F4/PNL-01): брак 15.09 в закрытом
        # сентябре менял его прибыль. Нашли поздно — вносите сегодняшним днём.
        _lock(data.get("happened_on"), "Записать отход этой датой")
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


class FifoRecalcView(APIView):
    """«Пересчитать себестоимость по FIFO» с даты (PNL-08, волна 2).

    POST /warehouse/fifo-recalc/preview/ {material, since} — что изменится;
    POST /warehouse/fifo-recalc/apply/ — провести. Только администратор; закрытый
    месяц — 400 с `closed_months` (как «Исправить приход»).
    """

    permission_classes = [IsAdmin]
    mode = "preview"

    def post(self, request):
        from .fifo_recalc import RecalcError, apply, preview

        material = get_object_or_404(Material, pk=request.data.get("material") or 0)
        raw = request.data.get("since")
        try:
            since = date.fromisoformat(str(raw)) if raw else timezone.localdate().replace(day=1)
        except ValueError:
            return Response({"detail": "Дата — в виде ГГГГ-ММ-ДД."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = apply(material, since, user=request.user) if self.mode == "apply" else preview(material, since)
        except RecalcError as e:
            body = {"detail": str(e)}
            if e.closed_months:
                body["closed_months"] = e.closed_months
            return Response(body, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)


class LotCorrectionView(APIView):
    """«Исправить приход» — опечатка в цене или количестве принятой партии.

    POST /warehouse/lot-correction/preview/ — что изменится (ничего не пишет);
    POST /warehouse/lot-correction/apply/   — провести одной транзакцией.

    Только администратор: правка меняет себестоимость проданного, закуп и долг
    поставщику. Закрытый месяц — 400 с перечнем месяцев (`closed_months`).
    """

    permission_classes = [IsAdmin]
    mode = "preview"

    def post(self, request):
        from .lot_correction import CorrectionError, apply, preview
        from .serializers import LotCorrectionSerializer

        serializer = LotCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        roll = data.pop("roll", None)
        line = data.pop("supply_line", None)
        try:
            if self.mode == "apply":
                result = apply(roll=roll, line=line, data=data, user=request.user)
            else:
                result = preview(roll=roll, line=line, data=data)
        except CorrectionError as e:
            body = {"detail": str(e)}
            if e.closed_months:
                body["closed_months"] = e.closed_months
            return Response(body, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)
