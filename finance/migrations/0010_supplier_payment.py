# Ссылка кассовой записи на партию: оплата поставщику при приёмке
# (2026-09-19).
#
# Та же история, что у `0008_cashentry_expense`: добавить внешний ключ к
# таблице, где уже есть строки, Postgres в одной транзакции не даёт — индекс
# упирается в «pending trigger events». Поэтому миграция БЕЗ транзакции и
# состоит ровно из одного действия: падать внутри ей нечем, а повторить её
# после сбоя можно только руками (см. комментарий в 0008).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("finance", "0009_cash_for_expenses"),
        ("warehouse", "0013_supplier_payment"),
    ]

    operations = [
        migrations.AddField(
            model_name="cashentry",
            name="roll",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="cash_entries", to="warehouse.roll",
                verbose_name="партия",
            ),
        ),
    ]
