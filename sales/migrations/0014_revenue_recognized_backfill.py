# Дата признания выручки для уже заведённых заказов (2026-10-07).
#
# Всем заказам — дата заказа: так их выручка и считалась, прошлые месяцы не
# двигаются. Исключение — НЕОПЛАЧЕННЫЙ онлайн-заказ (оплата не подтверждена,
# склад не списан): это ещё не продажа, поле остаётся пустым (решение D-7).
# Оплаченные онлайн-заказы получают дату заказа: момента подтверждения система
# раньше не хранила, а сдвигать их выручку задним числом незачем. На копии прода
# от 19.09 неоплаченных онлайн-заказов нет.
#
# Откат очищает поле (колонку удаляет откат 0013).

from django.db import migrations
from django.db.models import F, Q


def forward(apps, schema_editor):
    Receipt = apps.get_model("sales", "Receipt")
    unpaid_online = Q(payment_method="ONLINE", payment_status="PENDING", stock_deducted=False)
    Receipt.objects.exclude(unpaid_online).update(revenue_recognized_at=F("created_at"))
    Receipt.objects.filter(unpaid_online).update(revenue_recognized_at=None)


def backward(apps, schema_editor):
    Receipt = apps.get_model("sales", "Receipt")
    Receipt.objects.update(revenue_recognized_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0013_receipt_revenue_recognized_at'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
