# Себестоимость строки чека: фиксируется при списании со склада.

from decimal import Decimal
from django.db import migrations, models


def estimate_existing(apps, schema_editor):
    """Оценка себестоимости для заказов, оформленных ДО появления поля.

    Точных данных для них нет (мы тогда не запоминали, из какой партии ушёл
    материал), поэтому берём текущую закупочную цену материала. Для рулонных
    она равна себестоимости последней партии — приближение достаточное, чтобы
    прошлые месяцы не показывали завышенную прибыль. Новые продажи считаются
    точно, по FIFO.
    """
    TransactionItem = apps.get_model("sales", "TransactionItem")

    for item in TransactionItem.objects.select_related("material").filter(
        material__isnull=False, cost_total=0
    ):
        material = item.material
        qty = item.quantity or Decimal("0")
        # Продажа целым листом списывает площадь листа, а не «1 штуку».
        if item.sale_mode == "PIECE" and material.piece_area:
            qty = material.piece_area * qty
        item.cost_total = (qty * (material.purchase_price or Decimal("0"))).quantize(
            Decimal("0.01")
        )
        item.save(update_fields=["cost_total"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0009_receipt_title'),
    ]

    operations = [
        migrations.AddField(
            model_name='transactionitem',
            name='cost_total',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=14, verbose_name='себестоимость строки'),
        ),
        migrations.RunPython(estimate_existing, noop),
    ]
