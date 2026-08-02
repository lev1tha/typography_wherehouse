"""Закуп и долг материала становятся видами расхода с записями.

Раньше это были два числа в настройках — одна сумма на весь отчёт, без дат и
без расшифровки. Теперь они устроены как остальные строки: клик по строке
открывает список трат за период. Долг материала помечен `in_profit=False` —
он показан справочно и в итог не входит (материал в долг уже посчитан в
закупе), это тот же флаг, что у покупки оборудования.
"""

from decimal import Decimal

from django.db import migrations, models


def forward(apps, schema_editor):
    ExpenseKind = apps.get_model("finance", "ExpenseKind")
    ExpenseEntry = apps.get_model("finance", "ExpenseEntry")
    FinanceSettings = apps.get_model("finance", "FinanceSettings")

    purchase = ExpenseKind.objects.create(
        code="MATERIAL_PURCHASE", name="Закуп материала", block="MATERIALS",
        in_profit=True, position=5, is_builtin=True,
    )
    debt = ExpenseKind.objects.create(
        code="MATERIAL_DEBT", name="Долг материала", block="MATERIALS",
        in_profit=False, position=20, is_builtin=True,
    )

    row = FinanceSettings.objects.filter(pk=1).first()
    if not row:
        return
    # Старые суммы не теряем: каждая становится одной записью без даты покупки
    # (её в настройках не было) — датой ставим день, когда настройки правили.
    day = row.updated_at.date() if row.updated_at else None
    for kind, amount in ((purchase, row.material_purchase), (debt, row.material_debt)):
        if amount and amount > 0:
            entry = ExpenseEntry.objects.create(
                kind=kind, name="Перенесено из настроек", amount=amount,
            )
            if day:
                ExpenseEntry.objects.filter(pk=entry.pk).update(spent_at=day)


def backward(apps, schema_editor):
    ExpenseKind = apps.get_model("finance", "ExpenseKind")
    ExpenseEntry = apps.get_model("finance", "ExpenseEntry")
    FinanceSettings = apps.get_model("finance", "FinanceSettings")

    row, _created = FinanceSettings.objects.get_or_create(pk=1)
    for code, field in (("MATERIAL_PURCHASE", "material_purchase"), ("MATERIAL_DEBT", "material_debt")):
        total = sum(
            (e.amount for e in ExpenseEntry.objects.filter(kind__code=code)), Decimal("0")
        )
        setattr(row, field, total)
    row.save()
    ExpenseEntry.objects.filter(kind__code__in=["MATERIAL_PURCHASE", "MATERIAL_DEBT"]).delete()
    ExpenseKind.objects.filter(code__in=["MATERIAL_PURCHASE", "MATERIAL_DEBT"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0006_expense_kinds"),
    ]

    operations = [
        # Данные переносим ДО удаления полей.
        migrations.RunPython(forward, backward),
        migrations.RemoveField(model_name="financesettings", name="material_purchase"),
        migrations.RemoveField(model_name="financesettings", name="material_debt"),
        # Транспорт переехал в виды расхода ещё в 0006 — поле осталось мёртвым.
        migrations.RemoveField(model_name="financesettings", name="transport"),
    ]
