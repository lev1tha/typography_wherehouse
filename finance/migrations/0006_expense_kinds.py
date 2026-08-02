"""Виды расхода становятся справочником, а траты — одной таблицей.

Раньше строки финотчёта были зашиты двумя перечислениями (`FixedExpense.Category`
и `Expense.Category`), а траты лежали в трёх моделях с одинаковыми полями. Теперь
виды заводит админ, поэтому справочник `ExpenseKind` + единая `ExpenseEntry`.
Все существующие записи переносятся во встроенные виды один-в-один; зарплата
становится видом SALARY, где имя сотрудника — это `name` записи.
"""

import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models

# (код, название, блок, входит в прибыль, порядок в блоке)
BUILTIN_KINDS = [
    ("TRANSPORT", "Транспортные расходы", "MATERIALS", True, 10),
    ("RENT", "Аренда цеха", "FIXED", True, 10),
    ("UTILITIES", "Коммунальные услуги", "FIXED", True, 20),
    ("INTERNET", "Интернет", "FIXED", True, 30),
    ("SALARY", "Зарплаты", "FIXED", True, 40),
    ("FIXED_OTHER", "Прочие постоянные", "FIXED", True, 50),
    ("CUTTER", "Расходники (фреза)", "VARIABLE", True, 10),
    ("VAR_OTHER", "Прочие расходы", "VARIABLE", True, 20),
    # Вложения: видны в блоке, но прибыль не уменьшают (решение заказчика).
    ("EQUIPMENT", "Покупка оборудования", "VARIABLE", False, 30),
    ("IMPROVEMENT", "Улучшение цеха", "VARIABLE", False, 40),
]

FIXED_MAP = {
    "RENT": "RENT",
    "UTILITIES": "UTILITIES",
    "INTERNET": "INTERNET",
    "OTHER": "FIXED_OTHER",
}
VARIABLE_MAP = {
    "CUTTER": "CUTTER",
    "TRANSPORT": "TRANSPORT",
    "EQUIPMENT": "EQUIPMENT",
    "IMPROVEMENT": "IMPROVEMENT",
    "OTHER": "VAR_OTHER",
}


def forward(apps, schema_editor):
    ExpenseKind = apps.get_model("finance", "ExpenseKind")
    ExpenseEntry = apps.get_model("finance", "ExpenseEntry")
    Expense = apps.get_model("finance", "Expense")
    FixedExpense = apps.get_model("finance", "FixedExpense")
    SalaryPayment = apps.get_model("finance", "SalaryPayment")

    kinds = {
        code: ExpenseKind.objects.create(
            code=code, name=name, block=block, in_profit=in_profit,
            position=position, is_builtin=True,
        )
        for code, name, block, in_profit, position in BUILTIN_KINDS
    }

    def move(rows, kind_of, name_of, date_of):
        ExpenseEntry.objects.bulk_create([
            ExpenseEntry(
                kind=kinds[kind_of(r)],
                name=name_of(r),
                amount=r.amount,
                spent_at=date_of(r),
                note=r.note or "",
                created_by_id=r.created_by_id,
            )
            for r in rows
        ])

    move(
        FixedExpense.objects.all(),
        lambda r: FIXED_MAP.get(r.category, "FIXED_OTHER"),
        lambda r: r.name or "",
        lambda r: r.spent_at,
    )
    move(
        Expense.objects.all(),
        lambda r: VARIABLE_MAP.get(r.category, "VAR_OTHER"),
        lambda r: r.name or "",
        lambda r: r.spent_at,
    )
    # Зарплата: сотрудник переезжает в «за что / кому» — мастера и резчики не
    # заводятся как пользователи, это и раньше был свободный текст.
    move(
        SalaryPayment.objects.all(),
        lambda r: "SALARY",
        lambda r: r.employee,
        lambda r: r.paid_at,
    )


def backward(apps, schema_editor):
    ExpenseKind = apps.get_model("finance", "ExpenseKind")
    ExpenseEntry = apps.get_model("finance", "ExpenseEntry")
    Expense = apps.get_model("finance", "Expense")
    FixedExpense = apps.get_model("finance", "FixedExpense")
    SalaryPayment = apps.get_model("finance", "SalaryPayment")

    back_fixed = {v: k for k, v in FIXED_MAP.items()}
    back_variable = {v: k for k, v in VARIABLE_MAP.items()}

    for entry in ExpenseEntry.objects.select_related("kind"):
        code = entry.kind.code
        if code == "SALARY":
            SalaryPayment.objects.create(
                employee=entry.name, amount=entry.amount,
                paid_at=entry.spent_at, note=entry.note,
            )
        elif code in back_fixed:
            FixedExpense.objects.create(
                category=back_fixed[code], name=entry.name, amount=entry.amount,
                spent_at=entry.spent_at, note=entry.note,
            )
        else:
            # Свои виды в старой схеме места не имеют — кладём в «прочие».
            row = Expense.objects.create(
                category=back_variable.get(code, "OTHER"), name=entry.name,
                amount=entry.amount, note=entry.note,
            )
            # spent_at у старой модели auto_now_add, обычным create не задать.
            Expense.objects.filter(pk=row.pk).update(spent_at=entry.spent_at)

    ExpenseEntry.objects.all().delete()
    ExpenseKind.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0005_add_archive_and_transport'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ExpenseKind',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.SlugField(allow_unicode=True, help_text='Внутренний ключ. У встроенных видов постоянный, у своих — из названия.', max_length=40, unique=True, verbose_name='код')),
                ('name', models.CharField(max_length=120, verbose_name='название')),
                ('block', models.CharField(choices=[('MATERIALS', 'Материалы'), ('FIXED', 'Постоянные расходы'), ('VARIABLE', 'Переменные расходы')], max_length=12, verbose_name='блок отчёта')),
                ('in_profit', models.BooleanField(default=True, help_text='Снято — расход виден в отчёте, но прибыль не уменьшает (как покупка оборудования).', verbose_name='входит в прибыль')),
                ('is_builtin', models.BooleanField(default=False, verbose_name='встроенный')),
                ('position', models.PositiveIntegerField(default=100, verbose_name='порядок в блоке')),
                ('is_archived', models.BooleanField(default=False, verbose_name='скрыт')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'вид расхода',
                'verbose_name_plural': 'виды расходов',
                'ordering': ['block', 'position', 'id'],
            },
        ),
        migrations.CreateModel(
            name='ExpenseEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, max_length=255, verbose_name='за что / кому')),
                ('amount', models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=14, verbose_name='сумма')),
                ('spent_at', models.DateField(default=django.utils.timezone.localdate, verbose_name='дата')),
                ('note', models.TextField(blank=True, verbose_name='примечание')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expense_entries', to=settings.AUTH_USER_MODEL)),
                ('kind', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='entries', to='finance.expensekind', verbose_name='вид расхода')),
            ],
            options={
                'verbose_name': 'расход',
                'verbose_name_plural': 'расходы',
                'ordering': ['-spent_at', '-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='expenseentry',
            index=models.Index(fields=['kind', 'spent_at'], name='finance_exp_kind_id_67910a_idx'),
        ),
        # Данные переносим ДО удаления старых таблиц.
        migrations.RunPython(forward, backward),
        migrations.RemoveField(
            model_name='fixedexpense',
            name='created_by',
        ),
        migrations.RemoveField(
            model_name='salarypayment',
            name='created_by',
        ),
        migrations.DeleteModel(
            name='Expense',
        ),
        migrations.DeleteModel(
            name='FixedExpense',
        ),
        migrations.DeleteModel(
            name='SalaryPayment',
        ),
    ]
