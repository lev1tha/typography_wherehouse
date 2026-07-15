from django.db import migrations

INSTALL_KINDS = ["INSTALL_EXTERIOR", "INSTALL_INTERIOR", "INSTALLATION"]


def deactivate_installs(apps, schema_editor):
    """Установка убрана из системы (решение заказчика). Деактивируем существующие
    услуги установки — они пропадают из кассы, «Цен и услуг» и заявок, но строки
    не удаляем, чтобы не порвать историю чеков (TransactionItem.service = PROTECT)."""
    PrintingService = apps.get_model("services", "PrintingService")
    PrintingService.objects.filter(kind__in=INSTALL_KINDS).update(is_active=False)


def noop(apps, schema_editor):
    # Откат не воскрешает установку — это осознанное удаление функции.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0005_convert_recipe_applies_to_all"),
    ]

    operations = [
        migrations.RunPython(deactivate_installs, noop),
    ]
