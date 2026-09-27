"""Дата возврата у строк, возвращённых до появления поля.

Когда их возвращали, отчёты относили возврат к месяцу ЗАКАЗА — там он и
учтён в уже просмотренных цифрах. Ставим датой возврата дату заказа, чтобы
прошлые месяцы не сдвинулись от самой миграции. Новые возвраты датируются днём
оформления.

Отдельной миграцией от колонки: данные и схема в одной транзакции Postgres
иногда не пускает, а упавшая миграция при старте контейнера кладёт прод.
"""
from django.db import migrations
from django.db.models import OuterRef, Subquery


def backfill(apps, schema_editor):
    Item = apps.get_model("sales", "TransactionItem")
    Receipt = apps.get_model("sales", "Receipt")
    Item.objects.filter(is_returned=True, returned_at__isnull=True).update(
        returned_at=Subquery(
            Receipt.objects.filter(pk=OuterRef("receipt_id")).values("created_at")[:1]
        )
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0011_transactionitem_returned_at"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
