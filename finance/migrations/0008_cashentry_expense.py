# Ссылка кассовой записи на трату (проверка прод-данных 2026-09-19).
#
# Только колонка, без данных: их дописывает следующая миграция. Разделены они
# не для красоты — эта не может идти в транзакции (см. `atomic` ниже), а
# незавершённая миграция с данными оставила бы базу в состоянии, из которого
# повторный `migrate` уже не поднимется: колонка есть, записи в
# `django_migrations` нет, и AddField падает на «column already exists». На
# проде контейнер стартует через `migrate` (deploy/entrypoint.sh, set -e), то
# есть цех в этот момент просто не работает.

import django.db.models.deletion
from django.db import migrations, models


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
    ]
