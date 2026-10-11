"""Работы цеха: «Резка по станкам» целиком и выручка по видам услуг.

Две таблицы «Сводки» из одного списка строк-услуг:

- `machine_table` — по станкам: резка и «прочие работы» (гравировка, монтаж…)
  отдельными колонками, возврат — отдельной колонкой, а не вычитанием
  строки из отчёта, и ряд по дням (STAFF-03/-04);
- `by_service` — выручка и маржа по видам услуг: резка, гравировка, установка,
  буквы, отходы, прочее (PNL-06).

Деньги берутся тем же правилом, что выручка ОПиУ (`sales.reporting`): продажа
периода — днём признания выручки (в том числе возвращённая позже), возврат —
днём возврата, из какого бы заказа он ни был. Поэтому «нетто» по всем работам
плюс материал равно выручке ОПиУ.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from sales import reporting
from sales.models import TransactionItem
from services.models import PrintingService

from ..models import FinanceSettings
from ..periods import local_day
from .money import ZERO

Kind = PrintingService.Kind

# Виды услуг в разрезе выручки (PNL-06): ключ → какие `PrintingService.kind` в него входят.
SERVICE_GROUPS = (
    ("cutting", (Kind.CUTTING,)),
    ("engraving", (Kind.ENGRAVING,)),
    ("install", (Kind.INSTALLATION, Kind.INSTALL_INTERIOR)),
    ("letters", (Kind.INSTALL_EXTERIOR,)),
    ("waste", (Kind.WASTE,)),
    ("other", (Kind.OTHER,)),
)
GROUP_OF_KIND = {kind: key for key, kinds in SERVICE_GROUPS for kind in kinds}


def service_lines(d_from, d_to):
    """Строки-услуги периода: (знак, строка, день) — продажа «+», возврат «−».

    «+» — строки продаж периода (в том числе возвращённые когда угодно: в
    день продажи это была продажа), «−» — строки, возвращённые в периоде."""
    flt = {"type": TransactionItem.Type.SERVICE, "service__isnull": False}
    base = reporting._between(
        reporting.sold_lines(TransactionItem.objects.filter(is_returned=False, **flt)),
        reporting.LINE_SOLD_ON, d_from, d_to,
    )
    back = reporting.added_back(d_from, d_to).filter(**flt)
    out = reporting.returned_lines(d_from, d_to).filter(**flt)
    for sign, qs in ((1, base), (1, back), (-1, out)):
        for line in qs.select_related("service", "receipt", "leftover__material"):
            if sign > 0:
                day = local_day(line.receipt.revenue_recognized_at)
            else:
                day = local_day(line.returned_at)
            yield sign, line, day


def _slot():
    return {"sold": ZERO, "returned": ZERO, "meters": ZERO}


def machine_table(d_from, d_to) -> dict:
    """Работы по станкам: резка, прочие работы, возвраты, ряд по дням.

    Станок — `PrintingService.machine` услуги; у услуг без станка — «Без станка».
    Отходы — продажа обрезков, не работа станка — сюда не входят.
    """
    cutting = defaultdict(_slot)       # станок → резка
    other = defaultdict(_slot)         # станок → прочие работы
    days = defaultdict(lambda: {
        "machines": defaultdict(lambda: ZERO), "other": ZERO, "returned": ZERO, "sold": ZERO,
    })
    for sign, line, day in service_lines(d_from, d_to):
        svc = line.service
        if svc.kind == Kind.WASTE:
            continue
        amount = line.sold_total
        machine = svc.machine or ""
        is_cut = svc.kind == Kind.CUTTING
        slot = (cutting if is_cut else other)[machine]
        row = days[day]
        if sign > 0:
            slot["sold"] += amount
            if is_cut:
                slot["meters"] += line.quantity
                row["machines"][machine] += amount
            else:
                row["other"] += amount
            row["sold"] += amount
        else:
            slot["returned"] += amount
            row["returned"] += amount
    machines = set(cutting) | set(other)
    return {
        "cutting": {m: dict(cutting.get(m, _slot())) for m in machines},
        "other": {m: dict(other.get(m, _slot())) for m in machines},
        "days": [
            {
                "date": day,
                "machines": dict(data["machines"]),
                "other": data["other"],
                "returned": data["returned"],
                "total": data["sold"] - data["returned"],
            }
            for day, data in sorted(days.items())
            if data["sold"] or data["returned"]
        ],
    }


# --- Выручка и маржа по видам услуг ---------------------------------------------------------


def by_service(d_from, d_to, p) -> dict:
    """Выручка и маржа по видам услуг (PNL-06).

    Маржа строки = её стоимость − расходники по техкарте (`cost_total`). Если в
    настройках включено «учитывать долю мастера в марже строки» (PNL-05), из
    маржи вычитается и доля мастера. Только в этой таблице: в ОПиУ зарплата
    уже расходом, вторая копия задвоила бы её.
    """
    from .. import payroll

    include_share = FinanceSettings.load().master_share_in_margin
    directory = payroll.Directory() if include_share else None
    groups = {key: {"revenue": ZERO, "cost": ZERO, "share": ZERO, "lines": 0} for key, _ in SERVICE_GROUPS}
    services = defaultdict(lambda: {"revenue": ZERO, "cost": ZERO, "share": ZERO})
    warranty_services = ZERO
    # «Отходы» с полки остатков (D-204): выручка по материалу куска. Строки
    # «Отходов» без остатка — отдельной суммой (`unlinked`).
    shelf = defaultdict(lambda: {"revenue": ZERO, "pieces": 0})
    shelf_unlinked = ZERO
    for sign, line, _day in service_lines(d_from, d_to):
        # Гарантийная переделка (волна 2) — не продажа вида услуг: её
        # себестоимость своей строкой, как в ОПиУ (`cogs_warranty`).
        if line.receipt.is_warranty:
            warranty_services += sign * (line.cost_total or ZERO)
            continue
        key = GROUP_OF_KIND.get(line.service.kind, "other")
        revenue = sign * line.sold_total
        cost = sign * (line.cost_total or ZERO)
        share = sign * payroll.line_master_share(line, directory) if include_share else ZERO
        for slot in (groups[key], services[line.service_id]):
            slot["revenue"] += revenue
            slot["cost"] += cost
            slot["share"] += share
        groups[key]["lines"] += sign
        if key == "waste":
            if line.leftover_id:
                slot = shelf[(line.leftover.material_id, line.leftover.material.name)]
                slot["revenue"] += revenue
                slot["pieces"] += sign * int(line.quantity)
            else:
                shelf_unlinked += revenue
    names = dict(PrintingService.objects.filter(id__in=services).values_list("id", "name"))
    kinds = dict(PrintingService.objects.filter(id__in=services).values_list("id", "kind"))

    def margin(slot):
        return slot["revenue"] - slot["cost"] - slot["share"]

    rows = []
    for key, _kinds in SERVICE_GROUPS:
        g = groups[key]
        if not (g["revenue"] or g["cost"] or g["lines"]):
            continue
        rows.append({
            "key": key, "revenue": g["revenue"], "cost": g["cost"], "master_share": g["share"],
            "margin": margin(g),
            "services": [
                {"id": sid, "name": names.get(sid, ""), "revenue": s["revenue"], "cost": s["cost"],
                 "master_share": s["share"], "margin": margin(s)}
                for sid, s in sorted(services.items(), key=lambda kv: -kv[1]["revenue"])
                if GROUP_OF_KIND.get(kinds.get(sid), "other") == key and (s["revenue"] or s["cost"])
            ],
            **({"shelf": {
                "materials": [
                    {"id": mid, "name": name, "revenue": slot["revenue"], "pieces": slot["pieces"]}
                    for (mid, name), slot in sorted(shelf.items(), key=lambda kv: -kv[1]["revenue"])
                    if slot["revenue"] or slot["pieces"]
                ],
                "unlinked": shelf_unlinked,
            }} if key == "waste" else {}),
        })
    services_revenue = sum((r["revenue"] for r in rows), ZERO)
    materials_margin = p["revenue_material"] - p["cogs_material"]
    return {
        "rows": rows,
        "materials": {
            "revenue": p["revenue_material"], "cost": p["cogs_material"], "margin": materials_margin,
        },
        # Себестоимость гарантийных переделок (материал и расходники работ) —
        # отдельной строкой, как в ОПиУ; `services` — её часть по работам.
        "warranty": {"cost": p["cogs_warranty"], "services": warranty_services},
        "master_share_included": include_share,
        "services_revenue": services_revenue,
        # Выручка, не пришедшая строками услуг и материала (ручные корректировки,
        # округление): должна быть нулевой, и интерфейс её не прячет, если нет.
        "unallocated": p["revenue"] - p["revenue_material"] - services_revenue,
    }
