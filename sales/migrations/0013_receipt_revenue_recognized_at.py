# Дата признания выручки (2026-10-07, решения D-7/D-14) — только колонка.
# Заполняет её следующая миграция (0014). Откат удаляет колонку.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0012_returned_at_backfill'),
    ]

    operations = [
        migrations.AddField(
            model_name='receipt',
            name='revenue_recognized_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='дата признания выручки'),
        ),
    ]
