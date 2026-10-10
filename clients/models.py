from decimal import Decimal

from django.contrib.auth.hashers import check_password, make_password
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Client(models.Model):
    """Customer — a physical person or an OSOO (company)."""

    class Type(models.TextChoices):
        PHYSICAL = "PHYSICAL", _("Физ. лицо")
        OSOO = "OSOO", _("ОСОО")

    type = models.CharField(max_length=20, choices=Type.choices, default=Type.PHYSICAL)
    full_name = models.CharField(_("ФИО"), max_length=255, null=True, blank=True)
    company_name = models.CharField(
        _("название компании"), max_length=255, null=True, blank=True
    )
    phone = models.CharField(_("телефон"), max_length=32, unique=True)
    # ИНН покупателя — только для ОсОО и только ради счёта на оплату: без него
    # бухгалтерия юрлица счёт не проведёт. У физлица не спрашиваем.
    inn = models.CharField(_("ИНН"), max_length=32, blank=True, default="")
    # Пароль клиентского портала (хеш). Пусто = ещё не выдан: пароль выдаёт
    # АДМИН из карточки клиента и сообщает его лично. Клиент сам себе пароль не
    # заводит — иначе кабинет захватил бы любой, кто знает чужой номер.
    # Никогда не хранится в открытом виде.
    portal_password = models.CharField(_("пароль портала"), max_length=255, blank=True, default="")
    # Версия учётных данных портала: входит в токен клиента (клейм `cv`).
    # Новый пароль или смена телефона (логина) её увеличивают — выданные ранее
    # токены перестают приниматься. Токен без клейма = версия 0.
    credentials_version = models.PositiveIntegerField(default=0, editable=False)
    telegram_chat_id = models.CharField(
        _("Telegram chat id"), max_length=64, null=True, blank=True
    )
    referred_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referrals",
        verbose_name=_("кого привёл"),
        help_text=_("Клиент, который привёл этого клиента"),
    )
    # Постоянная скидка клиента, % (2026-10-10, CLI-02). Касса подставляет её
    # сама при выборе клиента; задаёт и меняет — только админ. 0 — без скидки.
    discount_percent = models.DecimalField(
        _("скидка, %"), max_digits=5, decimal_places=2, default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=_("Подставляется в кассе автоматически. 0 — без скидки"),
    )
    # Лимит долга, сом (2026-10-10, CLI-03). Пусто — свой лимит не задан, тогда
    # действует общий из `ClientSettings.default_credit_limit` (а если и он
    # пуст — лимита нет, как и было). Лимит ничего не запрещает: касса получает
    # предупреждение `debt_over_limit`, решает человек (D-92).
    credit_limit = models.DecimalField(
        _("лимит долга, сом"), max_digits=14, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("Пусто — действует общий лимит из настроек клиентов"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("клиент")
        verbose_name_plural = _("клиенты")
        ordering = ["-created_at"]

    @property
    def display_name(self) -> str:
        if self.type == self.Type.OSOO:
            return self.company_name or self.phone
        return self.full_name or self.phone

    @property
    def is_telegram_linked(self) -> bool:
        return bool(self.telegram_chat_id)

    @property
    def has_password(self) -> bool:
        return bool(self.portal_password)

    @property
    def effective_credit_limit(self):
        """Лимит, который действует для клиента: свой или общий. None — нет."""
        if self.credit_limit is not None:
            return self.credit_limit
        return ClientSettings.load().default_credit_limit

    def set_password(self, raw: str) -> None:
        """Store a salted hash of the portal password (never the raw value)."""
        self.portal_password = make_password(raw)
        self.credentials_version = (self.credentials_version or 0) + 1

    def check_password(self, raw: str) -> bool:
        return bool(self.portal_password) and check_password(raw, self.portal_password)

    def __str__(self) -> str:
        return f"{self.display_name} ({self.phone})"


class ReferralChangeRequest(models.Model):
    """Заявка на смену реферера (`Client.referred_by`).

    Реферал залочен после установки: кладовщик не может изменить его напрямую,
    но может подать заявку, которую администратор одобряет или отклоняет.
    Администратор также может менять реферера напрямую, минуя очередь.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Ожидает")
        APPROVED = "APPROVED", _("Одобрено")
        REJECTED = "REJECTED", _("Отклонено")

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="referral_requests",
        verbose_name=_("клиент"),
    )
    # null => предложение убрать реферера.
    new_referred_by = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="+",
        null=True,
        blank=True,
        verbose_name=_("новый реферер"),
    )
    # Снимок текущего реферера на момент подачи заявки (для аудита).
    previous_referred_by = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        verbose_name=_("прежний реферер"),
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("кто запросил"),
    )
    reviewed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("кто рассмотрел"),
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(_("обоснование / причина"), blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("заявка на смену реферера")
        verbose_name_plural = _("заявки на смену реферера")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        target = self.new_referred_by.display_name if self.new_referred_by else "—"
        return f"{self.client.display_name} → {target} [{self.get_status_display()}]"


class ClientSettings(models.Model):
    """Общие правила клиентов — одна строка (pk=1).

    Лимит долга по умолчанию не задан: пока владелец не задал значение, касса
    ничего не предупреждает (D-92). Приём денег складовщиком включён, как и оплата
    по чеку (`/pay/`), — выключатель на случай, если владелец захочет иначе (D-96).
    «Давность долга для предупреждения» живёт в настройках цен
    (`PricingSettings.debt_warn_days`) — там её читает касса.
    """

    default_credit_limit = models.DecimalField(
        _("общий лимит долга, сом"), max_digits=14, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("Для клиентов без своего лимита. Пусто — лимита нет"),
    )
    # Складовщик принимает оплату долга и аванс (CLI-08). Включено (по
    # умолчанию, как и приём оплаты по чеку `/pay/`): запись идёт в журнал
    # действий с именем складовщика, админ видит её в кассе и может откатить;
    # назад датой, списание долга и аванс «задним числом» — только админ.
    # Выключено — деньги клиентов принимает один админ.
    storekeeper_takes_debt = models.BooleanField(
        _("складовщик принимает оплату долга"), default=True,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("настройки клиентов")
        verbose_name_plural = _("настройки клиентов")

    def __str__(self) -> str:
        return "Настройки клиентов"

    @classmethod
    def load(cls) -> "ClientSettings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class ClientAdvance(models.Model):
    """Аванс клиента без заказа (CLI-05): деньги приняты, продажи ещё нет.

    В кассе это приход (статья «Оплата от клиента», без чека), у клиента —
    сальдо в его пользу («мы должны»). Выручки нет: она появится, когда аванс
    зачтут в оплату заказа. `remaining` — сколько ещё не зачтено; зачёт идёт
    через `clients.advances` и пишет `BalanceOffset`.
    """

    class Method(models.TextChoices):
        CASH = "CASH", _("Наличные")
        MBANK = "MBANK", _("MBank")
        DEMIRBANK = "DEMIRBANK", _("DemirBank")

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="advances")
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2)
    remaining = models.DecimalField(_("не зачтено"), max_digits=14, decimal_places=2)
    method = models.CharField(_("способ"), max_length=20, choices=Method.choices, default=Method.CASH)
    paid_on = models.DateField(_("дата"), default=timezone.localdate)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Ошибочный аванс отменяют целиком (пока из него ничего не зачтено):
    # в кассе встречная запись, в акте обе строки.
    reverted_at = models.DateTimeField(_("отменён"), null=True, blank=True)
    # Аванс НА НАЧАЛО (переезд из Excel, волна 2): деньги приняли до системы,
    # кассовой записи у него нет — ни при внесении, ни при отмене.
    is_opening = models.BooleanField(_("входящий остаток"), default=False)

    class Meta:
        verbose_name = _("аванс клиента")
        verbose_name_plural = _("авансы клиентов")
        ordering = ["-paid_on", "-id"]

    def __str__(self) -> str:
        return f"Аванс {self.amount} — {self.client.display_name}"


class BalanceOffset(models.Model):
    """Зачёт денег клиента в оплату долга его заказа без движения кассы.

    Деньги лежат в кассе с прошлого раза (сдача с переплаченного заказа или
    аванс) — второй раз их не приносили. Запись нужна, чтобы акт сверки знал
    ДАТУ зачёта: у чека есть только итоговое `change_applied`.
    """

    class Source(models.TextChoices):
        CHANGE = "CHANGE", _("Сдача прошлых заказов")
        ADVANCE = "ADVANCE", _("Аванс")

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="offsets")
    receipt = models.ForeignKey(
        "sales.Receipt", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # Номер заказа — снимок: заказ могут удалить, а в акте строка должна остаться.
    order_number = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    advance = models.ForeignKey(
        ClientAdvance, on_delete=models.CASCADE, null=True, blank=True, related_name="uses",
    )
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2)
    used_on = models.DateField(_("дата зачёта"), default=timezone.localdate)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("зачёт")
        verbose_name_plural = _("зачёты")
        ordering = ["used_on", "id"]


class OpeningBalance(models.Model):
    """Входящий остаток клиента на дату переезда из Excel (XL-04/F6/CLI-06, волна 2).

    ДОЛГ на начало — отдельная сущность, не чек: он не выручка, не налог, не
    прибыль и не касса (выручку по нему признали ещё в Excel), но входит в долг
    и сальдо клиента, в акт сверки (входящее сальдо), в возраст долга (дата —
    дата переезда), в список должников и плитку «Долг». `remaining` — сколько
    ещё не оплачено; оплаты — `OpeningDebtPayment`.

    АВАНС на начало — обычный `ClientAdvance` с `is_opening` (без кассы); эта
    запись лишь помнит, откуда он взялся (`advance`).

    Ошибочный остаток отменяют целиком (`reverted_at`), пока по нему ничего не
    оплачено и из аванса ничего не зачтено.
    """

    class Kind(models.TextChoices):
        DEBT = "DEBT", _("Долг клиента")
        ADVANCE = "ADVANCE", _("Аванс клиента")

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="opening_balances")
    kind = models.CharField(_("вид"), max_length=10, choices=Kind.choices)
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2)
    remaining = models.DecimalField(
        _("не оплачено"), max_digits=14, decimal_places=2, default=Decimal("0"),
        help_text=_("Для долга — сколько ещё должны; у аванса — 0 (остаток в самом авансе)"),
    )
    as_of = models.DateField(_("дата переезда"))
    # Тот же день моментом (полдень по местному времени): возраст долга и
    # «самый старый долг» сравниваются с датами признания чеков (DateTime).
    as_of_at = models.DateTimeField(_("момент переезда"), editable=False)
    advance = models.OneToOneField(
        ClientAdvance, on_delete=models.PROTECT, null=True, blank=True,
        related_name="opening", verbose_name=_("аванс"),
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    # Одна вставка из Excel — одна партия: по ней видно, что пришло вместе.
    batch = models.CharField(_("партия загрузки"), max_length=40, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reverted_at = models.DateTimeField(_("отменён"), null=True, blank=True)

    class Meta:
        verbose_name = _("входящий остаток клиента")
        verbose_name_plural = _("входящие остатки клиентов")
        ordering = ["as_of", "id"]

    def save(self, *args, **kwargs):
        from datetime import datetime, time

        self.as_of_at = timezone.make_aware(datetime.combine(self.as_of, time(12, 0)))
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.get_kind_display()} на {self.as_of}: {self.amount} — {self.client.display_name}"


class OpeningDebtPayment(models.Model):
    """Оплата (или списание) входящего долга клиента.

    Деньги — приход в кассу по статье «Оплата от клиента» без чека
    (`cash_entry_id` — его запись, голым числом, чтобы клиенты не зависели от
    модели финансов: по нему сверка ОПиУ→ОДДС относит деньги к строке «Входящие
    остатки»). Списание (`WRITE_OFF`) кассы не трогает, а пишет расход
    «Безнадёжные долги» (`expense_id`), как списание долга по чеку.
    """

    class Method(models.TextChoices):
        CASH = "CASH", _("Наличные")
        MBANK = "MBANK", _("MBank")
        DEMIRBANK = "DEMIRBANK", _("DemirBank")
        WRITE_OFF = "WRITE_OFF", _("Списание долга")

    opening = models.ForeignKey(OpeningBalance, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2)
    method = models.CharField(_("способ"), max_length=20, choices=Method.choices, default=Method.CASH)
    paid_on = models.DateField(_("дата оплаты"), default=timezone.localdate)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    cash_entry_id = models.PositiveIntegerField(null=True, blank=True)
    expense_id = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # ОТМЕНА ОПЛАТЫ (D-141, волна 3). Запись не удаляется: оплата была, и акт
    # сверки за прошлый месяц её помнит; отмена — своей строкой своего дня.
    # Деньги — встречный расход в кассу сегодняшним днём (`cancel_cash_entry_id`,
    # исходный приход остаётся в книге); списание (`WRITE_OFF`) убирает свой
    # расход «Безнадёжные долги». Остаток долга растёт на сумму оплаты.
    cancelled_at = models.DateTimeField(_("отменена"), null=True, blank=True)
    cancelled_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    cancel_reason = models.CharField(_("причина отмены"), max_length=200, blank=True)
    cancel_cash_entry_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = _("оплата входящего долга")
        verbose_name_plural = _("оплаты входящего долга")
        ordering = ["paid_on", "id"]


class ClientPrice(models.Model):
    """Договорная цена клиента (CLI-02, часть; волна 2).

    Цена за единицу на услугу или материал ЭТОГО клиента — поверх каталога и
    скидки: касса подставляет её вместо каталожной, скидка клиента к такой
    строке не применяется (договорённость уже учитывает её), а минимум и
    срочность заказа — применяются, как у любой строки. Вписанная руками цена
    в кассе сильнее договорной.

    - Материал: `material` + `sale_mode` (лист/штука, кв.м, пог.м) — цена той
      единицы, в которой продают.
    - Работа: `service` (+ необязательно `material` — «резка ЧПУ по акрилу 3 мм»
      дороже, чем по форексу): сначала ищется пара «услуга + материал работы»,
      потом «услуга без материала». Ставка — как в каталоге услуги (за пог.м
      реза, кв.м, штуку или за работу); проходы её умножают.
    """

    class Mode(models.TextChoices):
        SQM = "SQM", _("За кв.м")
        PIECE = "PIECE", _("За лист / штуку")
        METER = "METER", _("За пог.м")

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="prices")
    service = models.ForeignKey(
        "services.PrintingService", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    material = models.ForeignKey(
        "warehouse.Material", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    sale_mode = models.CharField(_("единица материала"), max_length=10, choices=Mode.choices, blank=True)
    price = models.DecimalField(
        _("цена, сом"), max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    updated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("договорная цена клиента")
        verbose_name_plural = _("договорные цены клиентов")
        ordering = ["client_id", "service_id", "material_id", "sale_mode"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "service", "material", "sale_mode"], name="client_price_unique",
            ),
        ]

    def __str__(self) -> str:
        what = self.service or self.material
        return f"{self.client.display_name}: {what} — {self.price}"


class ReferralBonus(models.Model):
    """Начисление реферального бонуса (CLI-07).

    Бонус начисляется один раз за приведённого клиента — когда у того появился
    первый ОПЛАЧЕННЫЙ и не возвращённый заказ. Сумма — ставка на момент
    начисления, смена ставки прошлое не меняет. Выплата — только запись
    «выплачено столько-то такого-то числа»: в расходы бонус не списывается
    (решение заказчика, бонус показывается справочно).
    """

    referrer = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="bonus_rows")
    referred = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="bonus_as_referred")
    receipt = models.ForeignKey(
        "sales.Receipt", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    order_number = models.PositiveIntegerField(null=True, blank=True)
    amount = models.DecimalField(_("начислено"), max_digits=14, decimal_places=2)
    accrued_on = models.DateField(_("дата начисления"))
    paid_amount = models.DecimalField(
        _("выплачено"), max_digits=14, decimal_places=2, default=Decimal("0"),
    )
    paid_on = models.DateField(_("дата выплаты"), null=True, blank=True)
    paid_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # Заказ вернули до выплаты, или реферера сменили, — начисление снято.
    voided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("начисление бонуса")
        verbose_name_plural = _("начисления бонусов")
        ordering = ["-accrued_on", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["referred"], condition=models.Q(voided_at__isnull=True),
                name="referral_bonus_one_active_per_referred",
            ),
        ]
