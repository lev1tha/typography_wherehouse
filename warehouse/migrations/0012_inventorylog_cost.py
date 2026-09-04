"""Себестоимость движения в журнале склада.

Нужна списанию и отходу: «сколько денег выбросили» иначе не узнать — у продажи
эта цифра лежит на строке чека, у брака строки чека нет. Заполняется списанием
по партиям (FIFO) и списанием с рулона; у прихода и у старых записей пусто."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("warehouse", "0011_alter_roll_form")]

    operations = [
        migrations.AddField(
            model_name="inventorylog",
            name="cost",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=14, null=True,
                verbose_name="себестоимость движения",
            ),
        ),
    ]
