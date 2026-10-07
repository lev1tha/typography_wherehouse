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
        # Ниже операционной прибыли: проценты по займам и уплата налога
        # (2026-10-07). Только встроенные виды — своих здесь не заводят.
        BELOW = "BELOW", _("Проценты и налоги")

    # Свои виды можно завести в этих блоках: в «Инвестициях» — покупкой
    # (капвложение), в остальных — операционным расходом. Роль вида выводится из
    # блока (`role_for_block`), галочки «входит в прибыль» больше нет.
    USER_BLOCKS = (Block.MATERIALS, Block.FIXED, Block.VARIABLE, Block.INVESTMENT)

    class Role(models.TextChoices):
        """Как трата этого вида ложится в отчёты — одна роль на вид.

        Строку ОПиУ и ОДДС для каждой роли задаёт справочник `finance.chart`.
        Раньше это решала пара «блок + входит в прибыль», и свой вид со снятой
        галочкой уходил из кассы, ни разу не появившись в ОПиУ (аудит, Б-10).
        """

        OPEX = "OPEX", _("Операционный расход")
        CAPEX = "CAPEX", _("Капвложение (амортизация)")
        INTEREST = "INTEREST", _("Проценты по займам")
        TAX = "TAX", _("Уплата налога")
        # Закуп материала: деньги ушли в склад, в прибыль они попадут
        # себестоимостью проданного, а не тратой.
        INVENTORY = "INVENTORY", _("Закуп в склад")
        # «Долг материала»: запись без денег и без расхода.
        NOT_CASH = "NOT_CASH", _("Справочно, без денег")

    code = models.SlugField(
        _("код"), max_length=40, unique=True, allow_unicode=True,
        help_text=_("Внутренний ключ. У встроенных видов постоянный, у своих — из названия."),
    )
    name = models.CharField(_("название"), max_length=120)
    block = models.CharField(_("блок отчёта"), max_length=12, choices=Block.choices)
    role = models.CharField(
        _("роль в отчётах"), max_length=12, choices=Role.choices, default=Role.OPEX,
        help_text=_("Куда трата ложится в ОПиУ и ОДДС — см. finance.chart."),
    )
    # Устарело с 2026-10-07: источник истины — `role`. Поле живёт ради отката
    # миграций и держится в согласии с ролью (`save`): прибыль уменьшают только
    # операционные расходы и проценты.
    in_profit = models.BooleanField(
        _("входит в прибыль"), default=True,
        help_text=_("Устарело: выводится из роли вида."),
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
    EQUIPMENT = "EQUIPMENT"
    IMPROVEMENT = "IMPROVEMENT"
    TAX = "TAX"
    INTEREST = "INTEREST"

    # Роли, траты которых уменьшают прибыль напрямую (капвложения — через
    # амортизацию, её считает отчёт). По ним же держится устаревший `in_profit`.
    PROFIT_ROLES = (Role.OPEX, Role.INTEREST)

    def __str__(self) -> str:
        return self.name

    @classmethod
    def role_for_block(cls, block) -> str:
        """Роль своего вида по блоку: «Инвестиции» — покупка, остальное — расход."""
        return cls.Role.CAPEX if block == cls.Block.INVESTMENT else cls.Role.OPEX

    def save(self, *args, **kwargs):
        # Новый вид в «Инвестициях», заведённый без роли, — покупка: так блок
        # и означал до появления ролей.
        if self._state.adding and self.block == self.Block.INVESTMENT and self.role == self.Role.OPEX:
            self.role = self.Role.CAPEX
        self.in_profit = self.role in self.PROFIT_ROLES
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "role" in update_fields:
            kwargs["update_fields"] = {*update_fields, "in_profit"}
        super().save(*args, **kwargs)

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

    Каждая трата, кроме «долга материала», ПИШЕТСЯ В КАССОВУЮ КНИГУ расходом
    (`finance.cash.sync_expense`). До 2026-09-19 не писалась ни одна: касса
    знала только приход, показывала «в ящике 245 453» и не знала про 86 877
    зарплат и аренды, уже вынутых оттуда. Долг материала — исключение: это
    запись «материал взяли, деньги ещё не отдали», и расхода по ней нет.
    """

    class Account(models.TextChoices):
        CASH = "CASH", _("Наличные")
        BANK = "BANK", _("Банк")

    kind = models.ForeignKey(
        ExpenseKind, on_delete=models.PROTECT, related_name="entries", verbose_name=_("вид расхода")
    )
    # Откуда заплатили. Нужно кассовой книге: аренду отдают из ящика, зарплату
    # часто переводом, и складывать их в один остаток нельзя — в ящике окажется
    # не то, что показывает система.
    account = models.CharField(
        _("чем заплатили"), max_length=10, choices=Account.choices, default=Account.CASH,
    )
    # Для зарплат сюда пишется имя сотрудника: мастера и резчики не заводятся
    # как пользователи системы, поэтому это свободный текст, а не ссылка.
    name = models.CharField(_("за что / кому"), max_length=255, blank=True)
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2, default=Decimal("0"))
    # Дату ставит пользователь: расходы часто вносят задним числом («аренда за
    # прошлый месяц»). Это дата ОПЛАТЫ — по ней трата уходит в кассовую книгу.
    spent_at = models.DateField(_("дата"), default=timezone.localdate)
    # За какой месяц расход — по нему трата ложится в ОПиУ (метод начисления,
    # 2026-10-07). Аренда сентября, оплаченная 5 октября: оплата — октябрём в
    # кассе, расход — сентябрём в прибыли. Хранится первым числом месяца; пусто
    # бывает только у записей до миграции finance/0014 — там месяц оплаты.
    period = models.DateField(
        _("за какой месяц"), null=True, blank=True,
        help_text=_("Первое число месяца, к которому относится расход."),
    )
    # Капвложение выше порога — актив: срок службы в месяцах, амортизация
    # равными долями со следующего месяца после покупки. Пусто — трата пошла в
    # расходы сразу (не капвложение или ниже порога). Решение «актив или расход»
    # запоминается здесь в момент ввода, и смена порога в настройках прошлое не
    # переписывает.
    useful_life_months = models.PositiveSmallIntegerField(
        _("срок службы, мес."), null=True, blank=True,
    )
    # Выбытие (поломка, списание): по этот месяц включительно амортизация идёт,
    # в нём же разом списывается остаток стоимости; дальше — ничего.
    depreciate_until = models.DateField(
        _("амортизировать до месяца"), null=True, blank=True,
        help_text=_("Первое число месяца выбытия. Пусто — до конца срока службы."),
    )
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

    @property
    def accrual_month(self):
        """Первое число месяца, к которому расход относится в ОПиУ."""
        from .periods import month_start

        return month_start(self.period or self.spent_at)

    @property
    def is_capitalized(self) -> bool:
        return self.useful_life_months is not None

    @staticmethod
    def life_cap(kind, spent_at):
        """Наибольший срок службы покупки, месяцев. None — без ограничения.

        Улучшение арендованного цеха служит не дольше аренды (D-13): месяцев от
        начала амортизации (следующий месяц после покупки) до месяца окончания
        аренды включительно, но не меньше одного. Аренда не указана — 60.
        Свои виды «Инвестиций» — как оборудование, без ограничения (D-26).
        """
        from .periods import add_months, months_between

        if kind.code != ExpenseKind.IMPROVEMENT:
            return None
        lease_until = FinanceSettings.load().lease_until
        if not lease_until:
            return DEFAULT_USEFUL_LIFE_MONTHS
        left = months_between(add_months(spent_at, 1), lease_until)
        return max(1, min(left, DEFAULT_USEFUL_LIFE_MONTHS))

    @classmethod
    def default_life(cls, kind, spent_at) -> int:
        return cls.life_cap(kind, spent_at) or DEFAULT_USEFUL_LIFE_MONTHS

    def save(self, *args, **kwargs):
        # «За какой месяц» всегда первым числом; не указан — месяц оплаты.
        from .periods import local_day, month_start

        self.spent_at = local_day(self.spent_at)
        # Капвложение, заведённое мимо формы (скриптом, миграцией, тестом), —
        # тем же правилом порога, что и в форме (D-22): иначе станок за
        # 300 000 молча стал бы расходом месяца.
        if (
            self._state.adding
            and self.useful_life_months is None
            and self.kind.role == ExpenseKind.Role.CAPEX
            and self.amount >= FinanceSettings.load().capitalization_threshold
        ):
            self.useful_life_months = self.default_life(self.kind, self.spent_at)
        self.period = month_start(self.period or self.spent_at)
        if self.depreciate_until:
            self.depreciate_until = month_start(self.depreciate_until)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "spent_at" in update_fields:
            kwargs["update_fields"] = {*update_fields, "period"}
        super().save(*args, **kwargs)


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
    клиенту, откат оплаты и — с 2026-09-19 — каждая трата из «Финансов»
    (аренда, зарплаты, коммуналка, вложения). Руками вносят то, чего система и
    правда не знает: инкассацию, внесение денег, пересчёт ящика.

    ОПЛАТА ПОСТАВЩИКУ пишется с 2026-09-19, но только когда при приёмке выбрали
    счёт: система сама знать не может, отдали за материал деньги или взяли в
    долг. Не выбрали — записи нет, как было раньше. Приходы, внесённые ДО этого
    дня (на проде 1 678 477 сом), задним числом не дописываются: деньги на них
    брались не из выручки, и касса ушла бы в минус на полтора миллиона, не
    объяснив, откуда они взялись. Остаток приводится к факту один раз, кнопкой
    «Пересчитать».

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
        # Финансовая деятельность в ОДДС: деньги владельца и займы. Не выручка
        # и не расход — прибыль они не трогают, а остаток кассы двигают, и без
        # своих статей «владелец забрал 50 000» читалось бы как «прочее».
        DEPOSIT = "DEPOSIT", _("Вложение владельца")
        OWNER_OUT = "OWNER_OUT", _("Изъятие владельцем")
        LOAN_IN = "LOAN_IN", _("Займ получен")
        LOAN_OUT = "LOAN_OUT", _("Займ погашен")
        COUNT = "COUNT", _("Пересчёт кассы")
        # Первое приведение кассы к факту (2026-10-07): деньги, которые лежали
        # в ящике и на счёте ДО того, как их начали вести в системе. Это не
        # движение денег, а остаток — в ОДДС вне потока, в ОПиУ его нет.
        # Обычный пересчёт (COUNT) — наоборот: недостача или излишек, и они
        # идут и в поток, и в прибыль.
        OPENING = "OPENING", _("Ввод начального остатка")
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
    #
    # SET_NULL, а не каскад: удаление заказа не стирает из книги деньги,
    # которые по нему приходили. Раньше каскад уносил приход целиком, и
    # удалённый оплаченный заказ на 700 молча уменьшал кассу без единой строки.
    # Теперь удаление пишет встречную запись (`sales.sale_service.delete_receipt`),
    # а обе строки остаются в книге с номером заказа в примечании.
    receipt = models.ForeignKey(
        "sales.Receipt", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="cash_entries", verbose_name=_("чек"),
    )
    supply = models.ForeignKey(
        "warehouse.Supply", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="cash_entries", verbose_name=_("накладная"),
    )
    # Трата, из-за которой деньги ушли. Правка суммы, даты или счёта в
    # «Финансах» двигает и эту запись, удаление траты — удаляет её.
    expense = models.ForeignKey(
        "finance.ExpenseEntry", on_delete=models.CASCADE, null=True, blank=True,
        related_name="cash_entries", verbose_name=_("трата"),
    )
    # Партия, за которую заплатили поставщику. Приход одной кнопкой документа
    # не заводит, и привязать оплату больше не к чему.
    #
    # SET_NULL, а не каскад (2026-10-07, аудит Б-13): удалили партию — оплата
    # остаётся в книге, а рядом пишется встречная запись сегодняшним днём
    # (`finance.cash.reverse_supplier_payments`). Каскад стирал расход прошлого
    # месяца, и ОДДС уже принятого месяца менялся без единой строки.
    roll = models.ForeignKey(
        "warehouse.Roll", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="cash_entries", verbose_name=_("партия"),
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
    # Амортизация (2026-10-07). Покупка вида «Инвестиции» дешевле порога —
    # сразу расход: растягивать 15 000 на 60 месяцев по 250 сом значит вести
    # учёт, который ничего не объясняет. 20 000 — решение владельца (D-22).
    capitalization_threshold = models.DecimalField(
        _("порог капвложения"), max_digits=14, decimal_places=2, default=Decimal("20000"),
        help_text=_("Покупка от этой суммы — актив с амортизацией, дешевле — сразу расход."),
    )
    # Улучшение арендованного цеха служит не дольше аренды: съехали — ремонт
    # остался хозяину. Срок такой покупки не больше месяцев до этой даты; пусто —
    # 60 месяцев, как у оборудования.
    lease_until = models.DateField(
        _("аренда помещения до"), null=True, blank=True,
        help_text=_("Ограничивает срок амортизации улучшений цеха. Пусто — 60 мес."),
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


# Срок службы по умолчанию, месяцев: оборудование и улучшение цеха (последнее —
# не дольше аренды, `FinanceSettings.lease_until`). Решение владельца, D-13.
DEFAULT_USEFUL_LIFE_MONTHS = 60


class TaxRate(models.Model):
    """Ставка налога с выручки и месяц, с которого она действует.

    История, а не одно число в настройках (2026-10-07, D-10): новая ставка
    начинает действовать со своего месяца, а прошлые месяцы считаются по своим
    ставкам — иначе смена 4 % на 3 % переписала бы прибыль всех прошлых лет.
    Налог месяца = ставка, действующая на первое число месяца, × выручка месяца
    нетто возвратов. До первой записи налога нет (решение D-19: с 10.2026).
    """

    valid_from = models.DateField(
        _("действует с месяца"), unique=True,
        help_text=_("Первое число месяца, с которого действует ставка."),
    )
    rate = models.DecimalField(
        _("ставка, %"), max_digits=5, decimal_places=2,
        help_text=_("Процент от выручки месяца, например 4.00."),
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tax_rates",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("ставка налога")
        verbose_name_plural = _("ставки налога")
        ordering = ["valid_from"]

    def __str__(self) -> str:
        return f"{self.rate} % с {self.valid_from:%m.%Y}"

    def save(self, *args, **kwargs):
        from .periods import month_start

        self.valid_from = month_start(self.valid_from)
        super().save(*args, **kwargs)

    @classmethod
    def rate_for(cls, month) -> Decimal:
        """Ставка (в процентах) месяца, в который попадает дата. Нет — 0."""
        from .periods import month_start

        row = (
            cls.objects.filter(valid_from__lte=month_start(month))
            .order_by("-valid_from").values_list("rate", flat=True).first()
        )
        return row if row is not None else Decimal("0")
