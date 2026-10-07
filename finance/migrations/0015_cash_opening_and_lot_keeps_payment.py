# Касса (2026-10-07):
#   · статья «Ввод начального остатка» (OPENING) — первое приведение кассы к
#     факту, в ОДДС вне потока (решение D-5);
#   · удаление партии больше не стирает её оплату поставщику: ссылка SET_NULL,
#     а встречную запись пишет код (аудит Б-13).
#
# Обе правки — на уровне Django: choices и on_delete в базе не живут, SQL на
# Postgres пустой (проверено `sqlmigrate`). Откат возвращает прежние choices и
# каскад; записи OPENING при откате остаются строками с неизвестной статьёй —
# перед откатом их нужно перевести в «Прочее» вручную.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0014_reports_v2_data'),
        ('warehouse', '0014_roll_supplier_debt'),
    ]

    operations = [
        migrations.AlterField(
            model_name='cashentry',
            name='article',
            field=models.CharField(choices=[('SALE', 'Оплата от клиента'), ('CHANGE', 'Сдача клиенту'), ('REFUND', 'Возврат клиенту'), ('UNPAY', 'Откат оплаты'), ('SUPPLY', 'Оплата поставщику'), ('EXPENSE', 'Расход цеха'), ('SALARY', 'Зарплата'), ('TRANSFER', 'Инкассация / перевод'), ('DEPOSIT', 'Вложение владельца'), ('OWNER_OUT', 'Изъятие владельцем'), ('LOAN_IN', 'Займ получен'), ('LOAN_OUT', 'Займ погашен'), ('COUNT', 'Пересчёт кассы'), ('OPENING', 'Ввод начального остатка'), ('OTHER', 'Прочее')], default='OTHER', max_length=20, verbose_name='статья'),
        ),
        migrations.AlterField(
            model_name='cashentry',
            name='roll',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cash_entries', to='warehouse.roll', verbose_name='партия'),
        ),
    ]
