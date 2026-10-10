"""Цены каталога: массовая переоценка, обновление пачкой и журнал «было → стало»
(XL-05, CALC-09, XL-07; волна 2).

Поставщик поднял акрил на 7 % — раньше это 78 ручных правок в карточках, и ни
одна не оставляла следа в журнале. Здесь:
- `plan_reprice` / `apply_changes` — «× %» по выбранным материалам с
  предпросмотром «было → стало» и округлением;
- `log_price_changes` — одна запись журнала (тип «Цены») на материал при любой
  правке цен: карточка, переоценка, пачка.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from audit.models import AuditLog

from .models import Material

# Поля цен и правил материала, правки которых пишутся в журнал.
PRICE_LABELS = {
    "price_per_sqm": "цена за кв.м",
    "piece_price": "цена за лист/шт",
    "price_per_unit": "цена за единицу",
    "price_per_pm": "цена за пог.м",
    "wholesale_price": "опт за лист",
    "wholesale_min_qty": "опт от, листов",
    "cut_rate_per_pm": "ставка резки за пог.м",
    "markup_percent": "наценка, %",
    "purchase_price": "закупочная цена",
    "price_tiers": "ступени опта",
}

# Что переоценивается «× %» по умолчанию — цены продажи (ставку резки и закуп
# не трогаем: это не прайс материала).
REPRICE_FIELDS = ("price_per_sqm", "piece_price", "price_per_unit", "price_per_pm", "wholesale_price")

# Что «Ввести пачкой» в режиме обновления меняет у существующего материала.
UPSERT_FIELDS = (
    "price_per_sqm", "piece_price", "price_per_unit", "price_per_pm", "cut_rate_per_pm",
    "wholesale_price", "wholesale_min_qty", "critical_balance",
)


def _fmt(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        text = f"{value:,.2f}".replace(",", " ")
        return text[:-3] if text.endswith(".00") else text
    return str(value)


def _tiers_text(material: Material) -> str:
    rows = sorted(material.price_tiers.all(), key=lambda t: t.min_qty)
    return "; ".join(f"от {_fmt(t.min_qty)} — {_fmt(t.price)}" for t in rows) or "—"


def snapshot(material: Material, fields=PRICE_LABELS) -> dict:
    return {
        f: (_tiers_text(material) if f == "price_tiers" else getattr(material, f))
        for f in fields
    }


def diff(before: dict, after: dict) -> list[tuple[str, object, object]]:
    return [(f, before[f], after[f]) for f in before if f in after and before[f] != after[f]]


def log_price_changes(user, material: Material, before: dict, after: dict, *, why: str = "") -> AuditLog | None:
    """«Цены «Акрил 3 мм»: цена за кв.м 1 650 → 1 815; цена за лист 4 900 → 5 390
    (переоценка +10 %)». Ничего не изменилось — записи нет."""
    changes = diff(before, after)
    if not changes:
        return None
    text = "; ".join(f"{PRICE_LABELS.get(f, f)} {_fmt(a)} → {_fmt(b)}" for f, a, b in changes)
    return AuditLog.record(
        user, f"Цены «{material.name}»: {text}" + (f" ({why})" if why else ""), kind="price",
    )


def round_to(value: Decimal, step: Decimal) -> Decimal:
    """Округление к ближайшему кратному `step` (1 сом, 10 сом)."""
    if not step or step <= 0:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return ((value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step).quantize(Decimal("0.01"))


def plan_reprice(materials, percent, *, fields=REPRICE_FIELDS, step=Decimal("1")) -> list[dict]:
    """Что станет с ценами: [{material, changes: [(поле, было, стало)]}].

    Нулевые цены не трогаем: ноль у `piece_price` значит «листом не
    продаётся», и × 1.1 его не должно оживлять.
    """
    factor = Decimal("1") + Decimal(percent) / Decimal("100")
    out = []
    for m in materials:
        changes = []
        for f in fields:
            old = getattr(m, f)
            if not old or old <= 0:
                continue
            new = round_to(old * factor, step)
            if new != old:
                changes.append((f, old, new))
        if changes:
            out.append({"material": m, "changes": changes})
    return out


@transaction.atomic
def apply_changes(plan: list[dict], user, *, why: str) -> int:
    """Провести план одной транзакцией; журнал — по записи на материал."""
    for row in plan:
        m = Material.objects.select_for_update().get(pk=row["material"].pk)
        before = snapshot(m)
        for f, _old, new in row["changes"]:
            setattr(m, f, new)
        m.save(update_fields=[f for f, *_ in row["changes"]] + ["updated_at"])
        log_price_changes(user, m, before, snapshot(m), why=why)
    return len(plan)


def plan_json(plan: list[dict]) -> list[dict]:
    return [
        {
            "id": row["material"].pk,
            "name": row["material"].name,
            "changes": [
                {"field": f, "label": PRICE_LABELS.get(f, f), "before": a, "after": b}
                for f, a, b in row["changes"]
            ],
        }
        for row in plan
    ]
