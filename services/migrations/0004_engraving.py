"""Новая услуга — гравировка (2026-09-04, просьба владельца).

Цена за кв.м гравируемой площади. Материал отдельной строкой не идёт: гравируют
либо материал клиента, либо лист, проданный своей строкой. Ставка 0 — её
задают в «Ценах и услугах»; в кассе цену за кв.м вписывают и админ, и
складовщик (у крупных заказов она своя, «5 000 за квадрат»).

Заводим услугу здесь, а не в seed: на проде seed больше не гоняют, а услуга
владельцу нужна сразу после обновления."""

from django.db import migrations, models


def add_engraving(apps, schema_editor):
    Service = apps.get_model("services", "PrintingService")
    if Service.objects.filter(kind="ENGRAVING").exists():
        return
    # Название — во всех языковых колонках: у модели включён modeltranslation,
    # интерфейс читает `name_ru`, и услуга с одним `name` показывалась бы
    # значением по умолчанию (см. 0003_cutting_machines).
    Service.objects.create(
        name="Гравировка",
        name_ru="Гравировка",
        name_ky="Гравировка",
        name_en="Engraving",
        kind="ENGRAVING",
        is_active=True,
    )


def drop_engraving(apps, schema_editor):
    Service = apps.get_model("services", "PrintingService")
    # Только нетронутую: на услугу с продажами ссылаются строки чеков (PROTECT).
    Service.objects.filter(kind="ENGRAVING", transaction_items__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [("services", "0003_cutting_machines")]

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
                ],
                default="CUTTING",
                max_length=20,
            ),
        ),
        migrations.RunPython(add_engraving, drop_engraving),
    ]
