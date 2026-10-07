# ОПиУ/ОДДС, вторая редакция (2026-10-07) — только КОЛОНКИ и новая таблица.
#
# Данные заполняет следующая миграция (0014): колонки и данные разными
# миграциями, как 0008/0009 — упади заполнение, повторный `migrate` не
# споткнётся о «column already exists» при старте контейнера.
#
# Что добавляется (все поля пустые или со значением по умолчанию — старые
# строки не меняются и отчёты считают как раньше):
#   ExpenseKind.role               — роль вида в отчётах (справочник finance.chart);
#   ExpenseEntry.period            — «за какой месяц» (ОПиУ по начислению);
#   ExpenseEntry.useful_life_months, depreciate_until — амортизация капвложений;
#   FinanceSettings.capitalization_threshold, lease_until — порог и срок аренды;
#   TaxRate                        — история ставки налога с выручки;
#   блок «Проценты и налоги» в choices вида (SQL — no-op).
#
# Откат: поля и таблица удаляются, данных в них до этой миграции не было.

import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0012_cash_financing_articles'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='expenseentry',
            name='depreciate_until',
            field=models.DateField(blank=True, help_text='Первое число месяца выбытия. Пусто — до конца срока службы.', null=True, verbose_name='амортизировать до месяца'),
        ),
        migrations.AddField(
            model_name='expenseentry',
            name='period',
            field=models.DateField(blank=True, help_text='Первое число месяца, к которому относится расход.', null=True, verbose_name='за какой месяц'),
        ),
        migrations.AddField(
            model_name='expenseentry',
            name='useful_life_months',
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='срок службы, мес.'),
        ),
        migrations.AddField(
            model_name='expensekind',
            name='role',
            field=models.CharField(choices=[('OPEX', 'Операционный расход'), ('CAPEX', 'Капвложение (амортизация)'), ('INTEREST', 'Проценты по займам'), ('TAX', 'Уплата налога'), ('INVENTORY', 'Закуп в склад'), ('NOT_CASH', 'Справочно, без денег')], default='OPEX', help_text='Куда трата ложится в ОПиУ и ОДДС — см. finance.chart.', max_length=12, verbose_name='роль в отчётах'),
        ),
        migrations.AddField(
            model_name='financesettings',
            name='capitalization_threshold',
            field=models.DecimalField(decimal_places=2, default=Decimal('20000'), help_text='Покупка от этой суммы — актив с амортизацией, дешевле — сразу расход.', max_digits=14, verbose_name='порог капвложения'),
        ),
        migrations.AddField(
            model_name='financesettings',
            name='lease_until',
            field=models.DateField(blank=True, help_text='Ограничивает срок амортизации улучшений цеха. Пусто — 60 мес.', null=True, verbose_name='аренда помещения до'),
        ),
        migrations.AlterField(
            model_name='expensekind',
            name='block',
            field=models.CharField(choices=[('MATERIALS', 'Материалы'), ('FIXED', 'Постоянные расходы'), ('VARIABLE', 'Переменные расходы'), ('INVESTMENT', 'Инвестиции'), ('BELOW', 'Проценты и налоги')], max_length=12, verbose_name='блок отчёта'),
        ),
        migrations.AlterField(
            model_name='expensekind',
            name='in_profit',
            field=models.BooleanField(default=True, help_text='Устарело: выводится из роли вида.', verbose_name='входит в прибыль'),
        ),
        migrations.CreateModel(
            name='TaxRate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('valid_from', models.DateField(help_text='Первое число месяца, с которого действует ставка.', unique=True, verbose_name='действует с месяца')),
                ('rate', models.DecimalField(decimal_places=2, help_text='Процент от выручки месяца, например 4.00.', max_digits=5, verbose_name='ставка, %')),
                ('note', models.CharField(blank=True, max_length=255, verbose_name='примечание')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tax_rates', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'ставка налога',
                'verbose_name_plural': 'ставки налога',
                'ordering': ['valid_from'],
            },
        ),
    ]
