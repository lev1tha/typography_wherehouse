# Постоянные расходы и зарплата: одна сумма в настройках → записи с датами.

import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


def settings_to_records(apps, schema_editor):
    """Переносим суммы из настроек в записи, чтобы не потерять введённое.

    Ставим дату первого числа текущего месяца — постоянные расходы месячные,
    и именно так их увидит месячный фильтр. Нулевые поля пропускаем.
    """
    FinanceSettings = apps.get_model("finance", "FinanceSettings")
    FixedExpense = apps.get_model("finance", "FixedExpense")
    SalaryPayment = apps.get_model("finance", "SalaryPayment")

    row = FinanceSettings.objects.first()
    if row is None:
        return

    today = django.utils.timezone.localdate()
    period_start = today.replace(day=1)
    label = f"{period_start:%m.%Y}"

    for field, category, title in (
        ("rent", "RENT", "Аренда"),
        ("utilities", "UTILITIES", "Коммунальные услуги"),
        ("internet", "INTERNET", "Интернет"),
        ("fixed_other", "OTHER", "Прочие постоянные"),
    ):
        amount = getattr(row, field, None) or Decimal("0")
        if amount <= 0:
            continue
        # Пояснения «что входит» были отдельными полями — переносим в примечание.
        note = ""
        if field == "utilities":
            note = getattr(row, "utilities_note", "") or ""
        elif field == "fixed_other":
            note = getattr(row, "fixed_other_note", "") or ""
        FixedExpense.objects.create(
            category=category,
            name=f"{title} за {label}",
            amount=amount,
            spent_at=period_start,
            note=note,
        )

    salary = getattr(row, "salary", None) or Decimal("0")
    if salary > 0:
        SalaryPayment.objects.create(
            employee="Перенесено из настроек",
            amount=salary,
            paid_at=period_start,
            note="Общая сумма зарплат до перехода на учёт по сотрудникам.",
        )


def records_to_settings(apps, schema_editor):
    """Откат: суммируем записи обратно в поля настроек."""
    FinanceSettings = apps.get_model("finance", "FinanceSettings")
    FixedExpense = apps.get_model("finance", "FixedExpense")
    SalaryPayment = apps.get_model("finance", "SalaryPayment")

    row = FinanceSettings.objects.first()
    if row is None:
        return

    def total(category):
        return sum(
            (e.amount for e in FixedExpense.objects.filter(category=category)),
            Decimal("0"),
        )

    row.rent = total("RENT")
    row.utilities = total("UTILITIES")
    row.internet = total("INTERNET")
    row.fixed_other = total("OTHER")
    row.salary = sum((s.amount for s in SalaryPayment.objects.all()), Decimal("0"))
    row.save()


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0003_financesettings_referral_bonus'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Порядок важен: сначала создаём таблицы, потом переносим в них данные,
        # и только затем удаляем старые поля — иначе переносить будет уже неоткуда.
        migrations.CreateModel(
            name='FixedExpense',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('category', models.CharField(choices=[('RENT', 'Аренда цеха'), ('UTILITIES', 'Коммунальные услуги'), ('INTERNET', 'Интернет'), ('OTHER', 'Прочие постоянные')], max_length=20)),
                ('name', models.CharField(blank=True, max_length=255, verbose_name='за что / период')),
                ('amount', models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=14, verbose_name='сумма')),
                ('spent_at', models.DateField(default=django.utils.timezone.localdate, verbose_name='дата')),
                ('note', models.TextField(blank=True, verbose_name='примечание')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='fixed_expenses', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'постоянный расход',
                'verbose_name_plural': 'постоянные расходы',
                'ordering': ['-spent_at', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='SalaryPayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('employee', models.CharField(max_length=255, verbose_name='сотрудник')),
                ('amount', models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=14, verbose_name='сумма')),
                ('paid_at', models.DateField(default=django.utils.timezone.localdate, verbose_name='дата выплаты')),
                ('note', models.TextField(blank=True, verbose_name='примечание')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='salary_payments', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'выплата зарплаты',
                'verbose_name_plural': 'зарплаты',
                'ordering': ['-paid_at', '-created_at'],
            },
        ),
        migrations.RunPython(settings_to_records, records_to_settings),
        migrations.RemoveField(model_name='financesettings', name='rent'),
        migrations.RemoveField(model_name='financesettings', name='utilities'),
        migrations.RemoveField(model_name='financesettings', name='utilities_note'),
        migrations.RemoveField(model_name='financesettings', name='internet'),
        migrations.RemoveField(model_name='financesettings', name='salary'),
        migrations.RemoveField(model_name='financesettings', name='fixed_other'),
        migrations.RemoveField(model_name='financesettings', name='fixed_other_note'),
    ]
