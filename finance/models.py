from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


class ExpenseKind(models.Model):
    """Вид расхода — одна строка финотчёта.

    Раньше виды были зашиты в код двумя перечислениями (`FixedExpense.Category`
    и `Expense.Category`), поэтому админ не мог завести «Рекламу» или «Налоги»
    без правки исходников. Теперь это справочник: встроенные виды создаёт
    миграция, свои добавляет админ.

    `block` — в каком из трёх блоков Excel-отчёта показывается строка.
    `in_profit` — уменьшает ли расход прибыль. Оборудование и улучшение цеха
    видны в отчёте, но прибыль не уменьшают: это инвестиции, станок за 300 000
    не должен делать месяц убыточным (решение заказчика).
    """

    class Block(models.TextChoices):
        MATERIALS = "MATERIALS", _("Материалы")
        FIXED = "FIXED", _("Постоянные расходы")
        VARIABLE = "VARIABLE", _("Переменные расходы")

    # Свои виды можно завести в любом блоке. В «Материалах» итог считается как
    # остаток на начало + Σ(строки блока с «входит в прибыль») − остаток на
    # конец, поэтому лишняя строка формулу не ломает: с флагом она добавится к
    # расходу материала, без флага останется справочной (как «долг материала»).
    USER_BLOCKS = (Block.MATERIALS, Block.FIXED, Block.VARIABLE)

    code = models.SlugField(
        _("код"), max_length=40, unique=True, allow_unicode=True,
        help_text=_("Внутренний ключ. У встроенных видов постоянный, у своих — из названия."),
    )
    name = models.CharField(_("название"), max_length=120)
    block = models.CharField(_("блок отчёта"), max_length=12, choices=Block.choices)
    in_profit = models.BooleanField(
        _("входит в прибыль"), default=True,
        help_text=_("Снято — расход виден в отчёте, но прибыль не уменьшает (как покупка оборудования)."),
    )
    # Встроенный вид нельзя удалить и нельзя перенести в другой блок: на его код
    # опирается отчёт (транспорт — в блоке «Материалы», зарплаты — по сотрудникам).
    is_builtin = models.BooleanField(_("встроенный"), default=False)
    position = models.PositiveIntegerField(_("порядок в блоке"), default=100)
    # Вид с записями не удаляется, а скрывается — иначе суммы прошлых месяцев
    # поехали бы задним числом (так же устроено скрытие материалов на складе).
    is_archived = models.BooleanField(_("скрыт"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("вид расхода")
        verbose_name_plural = _("виды расходов")
        ordering = ["block", "position", "id"]

    # Коды встроенных видов, на которые смотрит отчёт.
    TRANSPORT = "TRANSPORT"
    SALARY = "SALARY"
    MATERIAL_PURCHASE = "MATERIAL_PURCHASE"
    MATERIAL_DEBT = "MATERIAL_DEBT"

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def make_code(name: str) -> str:
        """Свободный код из названия («Реклама» → «реклама», «реклама-2», …)."""
        base = slugify(name or "", allow_unicode=True)[:32] or "vid"
        code, n = base, 2
        while ExpenseKind.objects.filter(code=code).exists():
            code = f"{base}-{n}"
            n += 1
        return code


class ExpenseEntry(models.Model):
    """Одна трата: вид, за что, сколько, когда.

    Единая таблица для всех трёх блоков — раньше постоянные расходы, покупки и
    зарплаты лежали в трёх разных моделях с одинаковыми полями.
    """

    kind = models.ForeignKey(
        ExpenseKind, on_delete=models.PROTECT, related_name="entries", verbose_name=_("вид расхода")
    )
    # Для зарплат сюда пишется имя сотрудника: мастера и резчики не заводятся
    # как пользователи системы, поэтому это свободный текст, а не ссылка.
    name = models.CharField(_("за что / кому"), max_length=255, blank=True)
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2, default=Decimal("0"))
    # Дату ставит пользователь: расходы часто вносят задним числом («аренда за
    # прошлый месяц»).
    spent_at = models.DateField(_("дата"), default=timezone.localdate)
    note = models.TextField(_("примечание"), blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expense_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("расход")
        verbose_name_plural = _("расходы")
        ordering = ["-spent_at", "-created_at"]
        indexes = [models.Index(fields=["kind", "spent_at"])]

    def __str__(self) -> str:
        return f"{self.kind.name}: {self.name} — {self.amount}"


class FinanceSettings(models.Model):
    """Singleton of manual P&L inputs that are not itemised expenses: material
    balances / purchase / supplier-debt. Computed values (stock-end, expenses,
    revenue, profit) are NOT stored — they are calculated live in the report
    endpoint."""

    # Материалы. Здесь остался только остаток на начало: это не трата, а
    # состояние склада. Закуп, транспорт и долг материала стали видами расхода
    # с записями по датам (ExpenseKind в блоке MATERIALS) — как остальные
    # строки отчёта, чтобы было видно, что именно покупали и у кого.
    # Пусто (null) — считается по складскому листу: Σ(остаток на начало месяца
    # по каждому материалу × его закупочная цена). Число — ручное значение,
    # которое побеждает расчёт. Ноль тут настоящий ноль, а не «не заполнено»,
    # поэтому именно null, а не 0.
    stock_start = models.DecimalField(
        _("остаток материалов на начало"), max_digits=14, decimal_places=2,
        null=True, blank=True,
        help_text=_("Пусто — считается по складу автоматически."),
    )
    # Реферальная программа
    referral_bonus = models.DecimalField(
        _("бонус за приведённого клиента"), max_digits=14, decimal_places=2, default=Decimal("0"),
        help_text=_("Фикс. сумма за каждого приведённого клиента. Только показывается в "
                    "карточке клиента — в расходы автоматически НЕ списывается."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("настройки финотчёта")
        verbose_name_plural = _("настройки финотчёта")

    def __str__(self) -> str:
        return "Настройки финотчёта"

    @classmethod
    def load(cls) -> "FinanceSettings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
