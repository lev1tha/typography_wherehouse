# Чем заплатили за накладную: ящик или счёт (2026-09-19).
#
# Пусто — не платили, взяли в долг: такая накладная в кассу не пишется. Сумму
# оплаты держит `paid_amount` (она бывает частичной), а это поле отвечает
# только на вопрос «откуда деньги».

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('warehouse', '0012_inventorylog_cost'),
    ]

    operations = [
        migrations.AddField(
            model_name='supply',
            name='paid_account',
            field=models.CharField(blank=True, choices=[('CASH', 'Наличные'), ('BANK', 'Банк')], max_length=10, verbose_name='чем заплатили'),
        ),
    ]
