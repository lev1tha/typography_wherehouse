# Блок «Инвестиции» (решение заказчика, 2026-08-24).
#
# Оборудование и улучшение цеха переезжают из «Переменных» в свой блок: они не
# должны ни уменьшать прибыль, ни сидеть в «Расходах». Закуп материала остаётся
# строкой блока «Материалы», но из прибыли уходит (флаг снят): деньги, вложенные
# в склад, — это оборот, прибыль они уменьшают по мере ПРОДАЖИ материала —
# строкой «Себестоимость проданного».

from django.db import migrations, models


def split_blocks(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    Kind.objects.filter(code__in=("EQUIPMENT", "IMPROVEMENT")).update(block="INVESTMENT")
    Kind.objects.filter(code="MATERIAL_PURCHASE").update(in_profit=False)


def merge_blocks(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    Kind.objects.filter(code__in=("EQUIPMENT", "IMPROVEMENT")).update(block="VARIABLE")
    Kind.objects.filter(code="MATERIAL_PURCHASE").update(in_profit=True)


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0005_periodlock'),
    ]

    operations = [
        migrations.AlterField(
            model_name='expensekind',
            name='block',
            field=models.CharField(choices=[('MATERIALS', 'Материалы'), ('FIXED', 'Постоянные расходы'), ('VARIABLE', 'Переменные расходы'), ('INVESTMENT', 'Инвестиции')], max_length=12, verbose_name='блок отчёта'),
        ),
        migrations.RunPython(split_blocks, merge_blocks),
    ]
