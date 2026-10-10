# Встроенный вид расходов WARRANTY «Гарантийные переделки» — в архив
# (волна 2, аудит «владелец против Excel»).
#
# С волны 2 себестоимость гарантийных переделок стоит своей строкой ОПиУ в
# себестоимости (`pnl.cogs_warranty`, по признаку `Receipt.is_warranty`). Вид
# расходов с тем же названием в блоке «Переменные» оставался пустой строкой
# таблицы ОПиУ и приглашал внести переделку тратой — то есть посчитать её
# второй раз. Скрытый вид в отчётах не показывается, пока по нему нет трат, и
# новую трату на него не завести.
#
# Только этот вид и только если по нему нет трат: если владелец уже вносил
# траты этого вида, вид остаётся видимым (его строки должны читаться как
# раньше). Ни одна прошлая цифра не меняется. Откат возвращает вид в работу.

from django.db import migrations


def archive(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    Entry = apps.get_model("finance", "ExpenseEntry")
    kind = Kind.objects.filter(code="WARRANTY", is_builtin=True, is_archived=False).first()
    if kind is None or Entry.objects.filter(kind=kind).exists():
        return
    kind.is_archived = True
    kind.save(update_fields=["is_archived"])


def unarchive(apps, schema_editor):
    Kind = apps.get_model("finance", "ExpenseKind")
    Kind.objects.filter(code="WARRANTY", is_builtin=True, is_archived=True).update(is_archived=False)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0018_ledger_payroll_assets"),
    ]

    operations = [
        migrations.RunPython(archive, unarchive),
    ]
