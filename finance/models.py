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
        # Инвестиции — отдельный блок, а не флаг внутри «Переменных» (решение
        # заказчика, 2026-08-24): станок и ремонт цеха не должны ни уменьшать
        # прибыль, ни сидеть в «Расходах» — это вложения, у них своя графа.
        INVESTMENT = "INVESTMENT", _("Инвестиции")

    # Свои виды можно завести в любом блоке. Строка с флагом «входит в прибыль»
    # добавится к итогу своего блока, без флага останется справочной (как «долг
    # материала»); в «Инвестициях» флаг всегда снят — блок в прибыль не входит.
    USER_BLOCKS = (Block.MATERIALS, Block.FIXED, Block.VARIABLE, Block.INVESTMENT)

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


class PeriodLock(models.Model):
    """Закрытый период: «по такое-то число трогать больше нельзя».

    Отчёт за июль, который владелец уже посмотрел и принял, мог назавтра
    показать другую цифру: даты заказов, трат и приходов правятся задним
    числом, а журнал действий это записывает, но не останавливает. В 1С месяц
    закрывают на замок — здесь так же.

    Одна дата, а не запись на каждый месяц: закрывают периоды подряд, и
    «закрыто по 31.07» — ровно та фраза, которой это называют вслух.
    """

    closed_through = models.DateField(
        _("закрыто по"), null=True, blank=True,
        help_text=_("Эта дата и всё, что раньше — только на чтение. Пусто — период открыт"),
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    updated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="period_locks",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("закрытие периода")
        verbose_name_plural = _("закрытие периода")

    def __str__(self) -> str:
        return f"Закрыто по {self.closed_through}" if self.closed_through else "Период открыт"

    @classmethod
    def load(cls) -> "PeriodLock":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class CashEntry(models.Model):
    """Кассовая книга: движение денег по кассе и по банку.

    Остатка денег в системе не было вовсе — были выручка, расходы и долги, то
    есть ОБОРОТЫ. На вопрос «сколько сейчас должно быть в ящике» ответить было
    нечем, а это то, чем в 1С закрывают день.

    Часть записей система пишет САМА: принятая оплата, выданная сдача, возврат
    клиенту, откат оплаты. Остальное вносят руками — закуп за наличные,
    зарплата, инкассация: этих событий система знать не может.

    Наличные и банк — один журнал с полем `account`, а не две таблицы: вопрос
    «сколько всего денег» задают чаще, чем «сколько именно в ящике», и склеивать
    две сущности ради него было бы лишней работой.
    """

    class Account(models.TextChoices):
        CASH = "CASH", _("Наличные")
        BANK = "BANK", _("Банк")

    class Kind(models.TextChoices):
        IN = "IN", _("Приход")
        OUT = "OUT", _("Расход")

    class Article(models.TextChoices):
        """Статья движения — «за что». Своих статей не заводим: их немного, и
        каждая означает конкретное событие, а не вкус пользователя."""

        SALE = "SALE", _("Оплата от клиента")
        CHANGE = "CHANGE", _("Сдача клиенту")
        REFUND = "REFUND", _("Возврат клиенту")
        UNPAY = "UNPAY", _("Откат оплаты")
        SUPPLY = "SUPPLY", _("Оплата поставщику")
        EXPENSE = "EXPENSE", _("Расход цеха")
        SALARY = "SALARY", _("Зарплата")
        TRANSFER = "TRANSFER", _("Инкассация / перевод")
        DEPOSIT = "DEPOSIT", _("Внесение денег")
        COUNT = "COUNT", _("Пересчёт кассы")
        OTHER = "OTHER", _("Прочее")

    account = models.CharField(
        _("счёт"), max_length=10, choices=Account.choices, default=Account.CASH
    )
    kind = models.CharField(_("тип"), max_length=10, choices=Kind.choices)
    article = models.CharField(
        _("статья"), max_length=20, choices=Article.choices, default=Article.OTHER
    )
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2)
    # Дата операции, а не момента ввода: деньги отдали в понедельник, до
    # компьютера дошли в четверг — как и везде в этой системе.
    happened_on = models.DateField(_("дата"), default=timezone.localdate)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    # Чем вызвана запись, если её сделала система. Ссылка, а не текст: из кассы
    # видно, по какому заказу пришли деньги, и наоборот.
    receipt = models.ForeignKey(
        "sales.Receipt", on_delete=models.CASCADE, null=True, blank=True,
        related_name="cash_entries", verbose_name=_("чек"),
    )
    supply = models.ForeignKey(
        "warehouse.Supply", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="cash_entries", verbose_name=_("накладная"),
    )
    # Запись создана системой, а не человеком: такие не правятся руками, иначе
    # касса разойдётся с чеками.
    is_auto = models.BooleanField(_("создана системой"), default=False)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="cash_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("кассовая операция")
        verbose_name_plural = _("кассовая книга")
        ordering = ["-happened_on", "-created_at"]
        indexes = [models.Index(fields=["account", "happened_on"])]

    def __str__(self) -> str:
        sign = "+" if self.kind == self.Kind.IN else "−"
        return f"{sign}{self.amount} ({self.get_article_display()})"

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.kind == self.Kind.IN else -self.amount

    @classmethod
    def balance(cls, account=None, *, upto=None, since=None) -> Decimal:
        """Остаток: приход минус расход. Без дат — «на сейчас»."""
        qs = cls.objects.all()
        if account:
            qs = qs.filter(account=account)
        if upto:
            qs = qs.filter(happened_on__lte=upto)
        if since:
            qs = qs.filter(happened_on__gte=since)
        total = Decimal("0")
        for kind, amount in qs.values_list("kind", "amount"):
            total += amount if kind == cls.Kind.IN else -amount
        return total


class CompanyProfile(models.Model):
    """Реквизиты цеха — шапка и подвал печатных документов.

    Отдельно от `FinanceSettings`: те закрыты от складовщика (там деньги), а
    реквизиты нужны как раз ему — накладную и товарный чек печатает он. Ничего
    секретного в них нет, эти же строки стоят на каждой выданной бумаге.

    Пустой профиль — не ошибка: пока заказчик не вписал реквизиты, документы
    печатаются без шапки, и в форме об этом сказано. Счёт без банка бесполезен,
    поэтому кнопка счёта на такой профиль не пускает.
    """

    name = models.CharField(
        _("название организации"), max_length=255, blank=True,
        help_text=_("Как в документах: ОсОО «...» или ИП Фамилия И.О."),
    )
    inn = models.CharField(_("ИНН"), max_length=32, blank=True)
    address = models.CharField(_("адрес"), max_length=255, blank=True)
    phone = models.CharField(_("телефон"), max_length=64, blank=True)
    bank_name = models.CharField(_("банк"), max_length=255, blank=True)
    bank_account = models.CharField(_("расчётный счёт"), max_length=64, blank=True)
    bik = models.CharField(_("БИК"), max_length=32, blank=True)
    director = models.CharField(
        _("руководитель"), max_length=255, blank=True,
        help_text=_("ФИО для строки подписи"),
    )
    accountant = models.CharField(_("бухгалтер"), max_length=255, blank=True)
    note = models.CharField(
        _("примечание в документах"), max_length=255, blank=True,
        help_text=_("Например «НДС не облагается» или срок оплаты счёта"),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("реквизиты организации")
        verbose_name_plural = _("реквизиты организации")

    def __str__(self) -> str:
        return self.name or "Реквизиты организации"

    @property
    def has_bank(self) -> bool:
        """Счёт на оплату без банка и счёта клиенту не пригодится."""
        return bool(self.bank_name and self.bank_account)

    @classmethod
    def load(cls) -> "CompanyProfile":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


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
