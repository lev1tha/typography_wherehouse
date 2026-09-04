"""Резка материала клиента: флаг «материал клиента» и комментарий к строке.

Клиент приносит своё, цех только режет — со склада ничего не уходит, в чеке
одна строка работы. Что именно резали, хранит комментарий: материала, по
которому строку узнают, у неё нет."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("sales", "0009_transactionitem_used_width")]

    operations = [
        migrations.AddField(
            model_name="transactionitem",
            name="own_material",
            field=models.BooleanField(
                default=False,
                help_text="Резали материал клиента — со склада ничего не списано",
                verbose_name="материал клиента",
            ),
        ),
        migrations.AddField(
            model_name="transactionitem",
            name="note",
            field=models.CharField(blank=True, max_length=255, verbose_name="комментарий"),
        ),
    ]
