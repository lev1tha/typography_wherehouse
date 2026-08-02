"""Остаток материалов на начало теперь считается сам, поле стало необязательным.

Пусто (NULL) — система считает остаток по складскому листу. Число — ручное
значение, которое побеждает расчёт. В старой схеме поле было обязательным с
дефолтом 0, и этот ноль означал ровно «не заполнено» (интерфейс так и писал —
«Заполните остаток на начало»). Поэтому существующие нули переводим в NULL,
иначе после обновления они читались бы как осознанно выставленный ноль и
глушили бы автоподсчёт.
"""

from decimal import Decimal

from django.db import migrations, models


def zeros_to_auto(apps, schema_editor):
    FinanceSettings = apps.get_model("finance", "FinanceSettings")
    FinanceSettings.objects.filter(stock_start=Decimal("0")).update(stock_start=None)


def auto_to_zeros(apps, schema_editor):
    FinanceSettings = apps.get_model("finance", "FinanceSettings")
    FinanceSettings.objects.filter(stock_start__isnull=True).update(stock_start=Decimal("0"))


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0007_material_kinds"),
    ]

    operations = [
        migrations.AlterField(
            model_name="financesettings",
            name="stock_start",
            field=models.DecimalField(
                blank=True, decimal_places=2,
                help_text="Пусто — считается по складу автоматически.",
                max_digits=14, null=True,
                verbose_name="остаток материалов на начало",
            ),
        ),
        migrations.RunPython(zeros_to_auto, auto_to_zeros),
    ]
