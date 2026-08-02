"""Встроенные производства — колонка «производство» из складского листа."""

from django.db import migrations

BUILTIN_SITES = [("BISHKEK", "Бишкек", 10), ("GLOBAL", "Глобал", 20)]


def create_sites(apps, schema_editor):
    ProductionSite = apps.get_model("warehouse", "ProductionSite")
    for code, name, position in BUILTIN_SITES:
        ProductionSite.objects.get_or_create(
            code=code, defaults={"name": name, "position": position, "is_builtin": True},
        )


def drop_sites(apps, schema_editor):
    ProductionSite = apps.get_model("warehouse", "ProductionSite")
    ProductionSite.objects.filter(code__in=[c for c, *_ in BUILTIN_SITES]).delete()


class Migration(migrations.Migration):

    dependencies = [("warehouse", "0002_builtin_material_types")]

    operations = [migrations.RunPython(create_sites, drop_sites)]
