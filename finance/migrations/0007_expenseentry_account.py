# Чем заплатили трату: из ящика или со счёта (проверка прод-данных 19.09.2026).
#
# Отдельной миграцией от ссылки `CashEntry.expense` не для красоты: добавление
# колонки переписывает таблицу трат, и Postgres в ТОЙ ЖЕ транзакции отказывается
# строить индекс внешнего ключа на кассовую книгу — «pending trigger events».
# Две миграции — две транзакции, и порядок соблюдён.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0006_alter_expensekind_block"),
    ]

    operations = [
        migrations.AddField(
            model_name="expenseentry",
            name="account",
            field=models.CharField(
                choices=[("CASH", "Наличные"), ("BANK", "Банк")],
                default="CASH", max_length=10, verbose_name="чем заплатили",
            ),
        ),
    ]
