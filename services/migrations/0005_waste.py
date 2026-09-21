"""Новая услуга — отходы (2026-09-21, просьба владельца).

Цех продаёт обрезки и брак, и мерка у каждой такой продажи своя: отходы бывают
от ЛЮБОГО товара на складе — от листа квадратами, от рулона метрами, от
штучного штуками. Поэтому мерку («кв.м / пог.м / шт») выбирают в кассе, а цену
там же вписывают — и админ, и складовщик: на отходы она всегда договорная.

Склада услуга не касается: отход уже списан там, где его признали браком
(«Отход (брак)» в приёмке) или где он остался обрезком от резки. Второе
списание увело бы остаток в минус.

Ставки в каталоге — три, по одной на мерку (`rate_flat` за кв.м, `rate_per_pm`
за пог.м, `rate_per_piece` за штуку). Заводим услугу здесь, а не в seed: на
проде seed не гоняют, а услуга владельцу нужна сразу после обновления.
"""

from django.db import migrations, models


def add_waste(apps, schema_editor):
    Service = apps.get_model("services", "PrintingService")
    if Service.objects.filter(kind="WASTE").exists():
        return
    # Название — во всех языковых колонках: у модели включён modeltranslation,
    # интерфейс читает `name_ru`, и услуга с одним `name` показывалась бы
    # значением по умолчанию (см. 0003_cutting_machines, 0004_engraving).
    Service.objects.create(
        name="Отходы",
        name_ru="Отходы",
        name_ky="Калдыктар",
        name_en="Waste",
        kind="WASTE",
        is_active=True,
    )


def drop_waste(apps, schema_editor):
    Service = apps.get_model("services", "PrintingService")
    # Только нетронутую: на услугу с продажами ссылаются строки чеков (PROTECT).
    Service.objects.filter(kind="WASTE", transaction_items__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [("services", "0004_engraving")]

    operations = [
        migrations.AlterField(
            model_name="printingservice",
            name="kind",
            field=models.CharField(
                choices=[
                    ("CUTTING", "Резка / работа мастера (по кв.м)"),
                    ("INSTALL_EXTERIOR", "Наружная установка (за букву)"),
                    ("INSTALL_INTERIOR", "Внутренняя установка (по кв.м)"),
                    ("INSTALLATION", "Установка (фикс)"),
                    ("OTHER", "Прочее (фикс)"),
                    ("ENGRAVING", "Гравировка (по кв.м)"),
                    ("WASTE", "Отходы (кв.м / пог.м / шт)"),
                ],
                default="CUTTING",
                max_length=20,
            ),
        ),
        migrations.RunPython(add_waste, drop_waste),
    ]
