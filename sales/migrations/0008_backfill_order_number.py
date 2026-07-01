from django.db import migrations


def backfill(apps, schema_editor):
    """Проставить сквозной order_number существующим чекам по дате создания
    (самый старый чек = №1)."""
    Receipt = apps.get_model("sales", "Receipt")
    n = 0
    for r in Receipt.objects.order_by("created_at", "id"):
        n += 1
        Receipt.objects.filter(pk=r.pk).update(order_number=n)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0007_receipt_order_number_alter_receipt_payment_method"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
