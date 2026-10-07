# ОПиУ/ОДДС, вторая редакция (2026-10-07) — ДАННЫЕ для колонок из 0013.
#
# 1. Роль каждому виду расхода (справочник finance.chart):
#      «Инвестиции»            → CAPEX (капвложение, в прибыль — амортизацией);
#      «Закуп материала»       → INVENTORY (уйдёт себестоимостью проданного);
#      «Долг материала»        → NOT_CASH (без денег и без расхода);
#      остальное               → OPEX.
#    `in_profit` НЕ трогаем — поле устарело и нужно только откату.
#    ВНИМАНИЕ: свой вид со снятым «входит в прибыль» вне «Инвестиций» получит
#    OPEX, то есть начнёт уменьшать прибыль. Перед выкладкой на прод такие виды
#    нужно посчитать (DEPLOY-чеклист в docs/FINANCE_PLAN.md) и, если найдутся,
#    спросить владельца, расход это или капвложение. На копии прода от 19.09
#    их нет.
# 2. Встроенные виды блока «Проценты и налоги»: «Проценты по займам» (INTEREST)
#    и «Налог (уплата)» (TAX).
# 3. «За какой месяц» всем старым тратам — месяц оплаты: прибыль прошлых
#    месяцев от этого не меняется.
# 4. Старым капвложениям — срок службы 60 мес., если сумма не ниже порога
#    (20 000), иначе пусто (сразу расход). На копии прода капвложений нет.
# 5. Ставка налога 4 % с октября 2026 (решение D-19): прошлые месяцы без налога.
#
# Откат: удаляет ставку и новые встроенные виды (если по ним уже есть траты —
# откат останавливается с объяснением), очищает заполненные поля.

from datetime import date
from decimal import Decimal

from django.db import migrations

TAX_FROM = date(2026, 10, 1)
TAX_RATE = Decimal("4.00")
LIFE = 60

NEW_KINDS = [
    # (код, название, роль, входит в прибыль, порядок)
    ("INTEREST", "Проценты по займам", "INTEREST", True, 10),
    ("TAX", "Налог (уплата)", "TAX", False, 20),
]


def forward(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    Entry = apps.get_model("finance", "ExpenseEntry")
    Settings = apps.get_model("finance", "FinanceSettings")
    TaxRate = apps.get_model("finance", "TaxRate")

    for kind in Kind.objects.all():
        if kind.block == "INVESTMENT":
            role = "CAPEX"
        elif kind.code == "MATERIAL_PURCHASE":
            role = "INVENTORY"
        elif kind.code == "MATERIAL_DEBT":
            role = "NOT_CASH"
        else:
            role = "OPEX"
        if kind.role != role:
            kind.role = role
            kind.save(update_fields=["role"])

    for code, name, role, in_profit, position in NEW_KINDS:
        Kind.objects.get_or_create(
            code=code,
            defaults={
                "name": name, "block": "BELOW", "role": role, "in_profit": in_profit,
                "is_builtin": True, "position": position,
            },
        )

    settings = Settings.objects.filter(pk=1).first()
    threshold = settings.capitalization_threshold if settings else Decimal("20000")
    for entry in Entry.objects.select_related("kind"):
        fields = []
        period = entry.spent_at.replace(day=1)
        if entry.period != period:
            entry.period = period
            fields.append("period")
        if entry.kind.role == "CAPEX" and entry.amount >= threshold and entry.useful_life_months is None:
            entry.useful_life_months = LIFE
            fields.append("useful_life_months")
        if fields:
            entry.save(update_fields=fields)

    TaxRate.objects.get_or_create(
        valid_from=TAX_FROM,
        defaults={"rate": TAX_RATE, "note": "4 % от выручки с октября 2026 — решение владельца"},
    )


def backward(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    Entry = apps.get_model("finance", "ExpenseEntry")
    TaxRate = apps.get_model("finance", "TaxRate")

    TaxRate.objects.filter(valid_from=TAX_FROM).delete()
    for code, *_ in NEW_KINDS:
        kind = Kind.objects.filter(code=code).first()
        if kind is None:
            continue
        if Entry.objects.filter(kind=kind).exists():
            raise RuntimeError(
                f"Откат finance/0014: по виду «{kind.name}» уже есть траты. "
                "Перенесите их в другой вид или удалите, затем повторите откат."
            )
        kind.delete()
    Entry.objects.update(period=None, useful_life_months=None, depreciate_until=None)
    Kind.objects.update(role="OPEX")


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0013_reports_v2_columns'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
