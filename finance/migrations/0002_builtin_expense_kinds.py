"""Встроенные виды расхода — строки финотчёта, без которых система не работает.

Это не перенос пользовательских данных, а справочник, на который опирается
отчёт: транспорт и закуп живут в блоке «Материалы» и участвуют в его формуле,
зарплаты собирают имена сотрудников, оборудование и улучшение цеха видны в
блоке, но прибыль не уменьшают (флаг `in_profit`). Коды постоянны — код на них
ссылается по имени, поэтому переименовать вид можно, а сменить код нельзя.

Свои виды («Реклама», «Налоги») админ добавляет сам через интерфейс.
"""

from django.db import migrations

# (код, название, блок, входит в прибыль, порядок в блоке)
BUILTIN_KINDS = [
    # Материалы: закуп считается по приходам на склад, транспорт и долг — руками.
    ("MATERIAL_PURCHASE", "Закуп материала", "MATERIALS", True, 5),
    ("TRANSPORT", "Транспортные расходы", "MATERIALS", True, 10),
    # Долг материала показан справочно: материал, взятый в долг, уже посчитан
    # в закупе, иначе задвоился бы.
    ("MATERIAL_DEBT", "Долг материала", "MATERIALS", False, 20),
    ("RENT", "Аренда цеха", "FIXED", True, 10),
    ("UTILITIES", "Коммунальные услуги", "FIXED", True, 20),
    ("INTERNET", "Интернет", "FIXED", True, 30),
    ("SALARY", "Зарплаты", "FIXED", True, 40),
    ("FIXED_OTHER", "Прочие постоянные", "FIXED", True, 50),
    ("CUTTER", "Расходники (фреза)", "VARIABLE", True, 10),
    ("VAR_OTHER", "Прочие расходы", "VARIABLE", True, 20),
    # Вложения: станок за 300 000 не должен делать месяц убыточным.
    ("EQUIPMENT", "Покупка оборудования", "VARIABLE", False, 30),
    ("IMPROVEMENT", "Улучшение цеха", "VARIABLE", False, 40),
]


def create_kinds(apps, schema_editor):
    ExpenseKind = apps.get_model("finance", "ExpenseKind")
    for code, name, block, in_profit, position in BUILTIN_KINDS:
        ExpenseKind.objects.get_or_create(
            code=code,
            defaults={
                "name": name, "block": block, "in_profit": in_profit,
                "position": position, "is_builtin": True,
            },
        )


def drop_kinds(apps, schema_editor):
    ExpenseKind = apps.get_model("finance", "ExpenseKind")
    ExpenseKind.objects.filter(code__in=[c for c, *_ in BUILTIN_KINDS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_kinds, drop_kinds),
    ]
