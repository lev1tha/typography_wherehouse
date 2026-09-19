# Траты попадают в кассовую книгу (проверка прод-данных 19.09.2026).
#
# Касса показывала 245 453 прихода и НИ ОДНОЙ выплаты, хотя трат в «Финансах»
# было внесено на 176 877 — зарплаты, аренда, коммуналка. Ответ на вопрос
# «сколько сейчас в ящике», ради которого книга и заводилась, был завышен на
# всю эту сумму.
#
# Здесь: у кассовой записи появляется ссылка на трату, а всем уже внесённым
# тратам дописывается расход — задним числом, датой самой траты. «Долг
# материала» пропускаем: это «материал взяли, деньги ещё не отдали», расхода по
# нему не было. Счёт у дописанных — тот, что стоит в трате (по умолчанию
# наличные); если платили переводом, это правится в самой трате, и кассовая
# запись переедет за ней.

import django.db.models.deletion
from django.db import migrations, models


def cash_for_expenses(apps, schema_editor):
    ExpenseEntry = apps.get_model("finance", "ExpenseEntry")
    CashEntry = apps.get_model("finance", "CashEntry")
    rows = []
    for entry in ExpenseEntry.objects.select_related("kind"):
        if entry.kind.code == "MATERIAL_DEBT":
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

    # Без общей транзакции. Postgres не даёт построить индекс внешнего ключа в
    # той же транзакции, где к таблице уже пристроили колонку: у неё остаются
    # «pending trigger events» — проверки ссылок по существующим строкам. В
    # одной транзакции миграция падает на ровном месте («cannot CREATE INDEX
    # … because it has pending trigger events»), причём только на живой базе с
    # данными: на пустой тестовой проверять нечего, и там она проходит.
    atomic = False

    dependencies = [
        ("finance", "0007_expenseentry_account"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashentry",
            name="expense",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="cash_entries", to="finance.expenseentry",
                verbose_name="трата",
            ),
        ),
        migrations.RunPython(cash_for_expenses, drop_cash_for_expenses),
    ]
