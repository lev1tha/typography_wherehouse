# Встроенные виды расходов для списаний, которые не были «тратой денег»
# (2026-10-10, аудит «владелец против Excel», волна 1).
#
#   BAD_DEBT  «Безнадёжные долги»      — долг клиента списан, денег не было;
#   WARRANTY  «Гарантийные переделки»  — переделка за свой счёт;
#   FX_DIFF   «Курсовая разница»       — разница курса при оплате поставщику.
#
# Все три — операционный расход (роль OPEX) в блоке «Переменные расходы»:
# зависят от объёма работы и продаж, постоянными не бывают, а «Прочих блоков»
# в ОПиУ нет. Блок — только место строки в отчёте, на сумму прибыли он не влияет.
#
# Это справочник, а не перенос данных: ни одна прошлая цифра не меняется (трат
# этих видов ещё нет). В отчёте вид виден нулевой строкой, как и остальные
# статьи «Переменных». Откат удаляет виды, если по ним нет трат; иначе откат
# останавливается с объяснением, а не стирает данные.

from django.db import migrations

NEW_KINDS = [
    # (код, название, порядок в блоке)
    ("BAD_DEBT", "Безнадёжные долги", 30),
    ("WARRANTY", "Гарантийные переделки", 40),
    ("FX_DIFF", "Курсовая разница", 50),
]


def add_kinds(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    for code, name, position in NEW_KINDS:
        Kind.objects.get_or_create(
            code=code,
            defaults={
                "name": name, "block": "VARIABLE", "role": "OPEX", "in_profit": True,
                "position": position, "is_builtin": True,
            },
        )


def drop_kinds(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    Entry = apps.get_model("finance", "ExpenseEntry")
    codes = [c for c, *_ in NEW_KINDS]
    if Entry.objects.filter(kind__code__in=codes).exists():
        raise RuntimeError(
            "Откат остановлен: по видам «Безнадёжные долги», «Гарантийные переделки» "
            "или «Курсовая разница» уже есть траты. Удалите их и повторите."
        )
    Kind.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0016_report_date_indexes"),
    ]

    operations = [
        migrations.RunPython(add_kinds, drop_kinds),
    ]
