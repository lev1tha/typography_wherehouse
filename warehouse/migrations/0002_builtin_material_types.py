"""Встроенные типы материала — то, чем режет этот цех.

Раньше типа не было: он сидел внутри названия («форекс 8мм»), а отчёт по резке
угадывал его подстрокой и на живой номенклатуре ошибался — «синий бишкек»,
«день ночь» и «салатовый» это акрил, но попадали в «Прочее».

Свой тип админ добавляет через интерфейс, как вид расхода в финотчёте.
"""

from django.db import migrations

# (код, название, порядок)
BUILTIN_TYPES = [
    ("FOREX", "Форекс", 10),
    ("ACRYL", "Акрил", 20),
    ("ORGGLASS", "Оргстекло", 30),
    ("ALUKOBOND", "Алюкобонд", 40),
    ("ROMARK", "Ромарк", 50),
    ("FILM", "Плёнка", 60),
    ("OTHER", "Прочее", 100),
]


def create_types(apps, schema_editor):
    MaterialType = apps.get_model("warehouse", "MaterialType")
    for code, name, position in BUILTIN_TYPES:
        MaterialType.objects.get_or_create(
            code=code,
            defaults={"name": name, "position": position, "is_builtin": True},
        )


def drop_types(apps, schema_editor):
    MaterialType = apps.get_model("warehouse", "MaterialType")
    MaterialType.objects.filter(code__in=[c for c, *_ in BUILTIN_TYPES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("warehouse", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_types, drop_types),
    ]
