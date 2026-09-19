# Уже внесённым тратам дописываем кассовый расход (проверка прод-данных
# 2026-09-19).
#
# Касса показывала 245 453 прихода и НИ ОДНОЙ выплаты, хотя трат в «Финансах»
# было внесено на 176 877 — зарплаты, аренда, коммуналка. Ответ на вопрос
# «сколько сейчас в ящике», ради которого книга и заводилась, был завышен на
# всю эту сумму. Новые траты пишет `finance.cash.sync_expense`, а этим —
# задним числом, датой самой траты.
#
# «Долг материала» пропускаем: это «материал взяли, деньги ещё не отдали»,
# расхода по нему не было. Счёт берём из траты (по умолчанию наличные); если
# платили переводом, это правится в самой трате, и запись переедет за ней.
#
# Транзакция здесь обычная: данные должны либо лечь целиком, либо не лечь
# вовсе. Колонку добавила предыдущая миграция — ей транзакция как раз
# противопоказана, поэтому они и разделены.

from django.db import migrations


def cash_for_expenses(apps, schema_editor):
    ExpenseEntry = apps.get_model("finance", "ExpenseEntry")
    CashEntry = apps.get_model("finance", "CashEntry")
    rows = []
    for entry in ExpenseEntry.objects.select_related("kind"):
        if entry.kind.code == "MATERIAL_DEBT":
            continue
        # Повторный прогон не должен удваивать выплаты: миграцию могли
        # откатить и накатить снова, а запись у траты всегда одна.
        if CashEntry.objects.filter(expense=entry).exists():
            continue
        name = f"{entry.kind.name}: {entry.name}".strip(": ") or entry.kind.name
        rows.append(CashEntry(
            account=entry.account,
            kind="OUT",
            article="SALARY" if entry.kind.code == "SALARY" else "EXPENSE",
            amount=entry.amount,
            happened_on=entry.spent_at,
            note=name[:255],
            expense=entry,
            created_by_id=entry.created_by_id,
            is_auto=True,
        ))
    CashEntry.objects.bulk_create(rows)


def drop_cash_for_expenses(apps, schema_editor):
    CashEntry = apps.get_model("finance", "CashEntry")
    CashEntry.objects.filter(expense__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0008_cashentry_expense"),
    ]

    operations = [
        migrations.RunPython(cash_for_expenses, drop_cash_for_expenses),
    ]
