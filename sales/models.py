import uuid
from decimal import ROUND_CEILING, Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Receipt(models.Model):
    """A sale/receipt that bundles several line items for one client."""

    class PaymentMethod(models.TextChoices):
        CASH = "CASH", _("Наличные")
        MBANK = "MBANK", _("MBank")
        DEMIRBANK = "DEMIRBANK", _("DemirBank")
        ONLINE = "ONLINE", _("Онлайн-оплата")

    class PaymentStatus(models.TextChoices):
        PENDING = "PENDING", _("Ожидает оплаты")
        PAID = "PAID", _("Оплачено")
        REFUNDED = "REFUNDED", _("Возвращено")
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", _("Частичный возврат")

    # Статусы, при которых по чеку ещё может быть долг клиента.
    OWING_STATUSES = (PaymentStatus.PENDING, PaymentStatus.PARTIALLY_REFUNDED)

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", _("Совершён")
        CANCELLED = "CANCELLED", _("Отменён/Возвращён")

    class FulfillmentStatus(models.TextChoices):
        PROCESSING = "PROCESSING", _("Готовится")
        READY = "READY", _("Готово к выдаче")
        ISSUED = "ISSUED", _("Выдан")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Человеческий сквозной номер заказа (№1, №2, …) — UUID остаётся внутренним
    # ключом (на него ссылаются позиции), а в чеках/портале показываем этот номер.
    order_number = models.PositiveIntegerField(
        _("номер заказа"), unique=True, null=True, blank=True, editable=False
    )
    # Человеческое название заказа («Вывеска для кафе») — чтобы в списке чеков
    # узнавать работу не только по номеру. Необязательное.
    title = models.CharField(_("наименование заказа"), max_length=255, blank=True)
    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.PROTECT,
        related_name="receipts",
        null=True,
        blank=True,
    )
    cashier = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipts",
        help_text=_("Складовщик, оформивший чек"),
    )
    payment_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.COMPLETED
    )
    fulfillment_status = models.CharField(
        _("статус выполнения"),
        max_length=20,
        choices=FulfillmentStatus.choices,
        default=FulfillmentStatus.PROCESSING,
        help_text=_("Готовится → Готово к выдаче → Выдан (для заказов с услугами)"),
    )
    total_price = models.DecimalField(
        _("итоговая стоимость"), max_digits=14, decimal_places=2, default=Decimal("0")
    )
    refunded_amount = models.DecimalField(
        _("сумма возврата"), max_digits=14, decimal_places=2, default=Decimal("0")
    )
    amount_paid = models.DecimalField(
        _("оплачено (предоплата)"), max_digits=14, decimal_places=2, default=Decimal("0")
    )
    # Сдача, которую клиенту ЕЩЁ НЕ ОТДАЛИ. Заказ на 1500, принесли 3000, а в
    # кассе не было мелочи — 1500 остались у цеха, и это долг цеха перед
    # клиентом, зеркальный обычному долгу. Раньше переплата просто отбрасывалась
    # (`min(amount_paid, total)`), и назавтра вспомнить, сколько за кем осталось,
    # было нечем.
    #
    # Отдельным полем, а НЕ через `amount_paid > total`: `amount_paid` участвует
    # в долге, статусе оплаты и во всех отчётах, и «оплачено 3000» по заказу на
    # 1500 сделало бы выручку неверной.
    change_due = models.DecimalField(
        _("сдача клиенту"),
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Переплата, которую ещё не вернули на руки"),
    )
    # Сколько этого заказа закрыто СДАЧЕЙ с прошлых заказов клиента. Денег при
    # этом никто не приносил — они уже лежат в кассе с того раза, — поэтому в
    # кассовую книгу зачёт не пишется, а вот объяснить «почему заказ оплачен,
    # если платили меньше» без этого поля нечем.
    change_applied = models.DecimalField(
        _("зачтено сдачей"),
        max_digits=14,
        decimal_places=2,
        default=Decimal("0"),
        help_text=_("Часть заказа, закрытая сдачей с прошлых заказов клиента"),
    )
    stock_deducted = models.BooleanField(
        _("склад списан"),
        default=False,
        help_text=_("Материал уже списан со склада — для корректного возврата."),
    )
    # Online payment gateway reference / link, filled when method is ONLINE.
    payment_reference = models.CharField(max_length=255, blank=True)
    payment_url = models.URLField(blank=True)
    # Дата заказа. НЕ auto_now_add: заказчик заносит работы задним числом —
    # заказ сделали на прошлой неделе, до компьютера дошли сегодня. Не указана —
    # текущий момент, как и было.
    #
    # Это ОПОРНАЯ дата всей отчётности: по ней считаются выручка, прибыль по
    # дням, себестоимость проданного и складской лист. Поэтому ставить её в
    # прошлое может только админ (см. checkout) — задним числом двигаются деньги
    # уже закрытых месяцев.
    created_at = models.DateTimeField(_("дата заказа"), default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("чек")
        verbose_name_plural = _("чеки")
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        # Проставляем человеческий сквозной номер при первом сохранении.
        # Домен маленький (1–2 кассира) — Max+1 достаточно; при равной гонке
        # unique-ограничение не даст двум чекам получить один номер.
        if self.order_number is None:
            last = Receipt.objects.aggregate(m=models.Max("order_number"))["m"] or 0
            self.order_number = last + 1
        super().save(*args, **kwargs)

    def recalculate_total(self) -> Decimal:
        # Use .filter() (not .all()) to bypass any stale prefetch cache — the
        # view loads receipts with prefetch_related, and дозаказ adds new items.
        total = sum(
            (item.line_total for item in self.items.filter(is_returned=False)),
            Decimal("0"),
        )
        self.total_price = total
        return total

    @property
    def has_service(self) -> bool:
        return self.items.filter(type=TransactionItem.Type.SERVICE).exists()

    @property
    def debt(self) -> Decimal:
        """Остаток к оплате: total − предоплата − возвраты. 0, если оплачен/отменён.

        Частично возвращённый чек тоже может быть должен: заказ на 979 в долг,
        клиент вернул работу на 148 — за материал на 831 он по-прежнему должен.
        Раньше долг считался только у PENDING, и после возврата одной строки
        остаток пропадал из карточки, плиток и списка (оплату при этом принять
        было можно — `apply_payment` долг видел). Статусы, при которых чек
        может быть должен, — `OWING_STATUSES`, ими же пользуются аннотации
        списков и отчёты.
        """
        if self.status == self.Status.CANCELLED:
            return Decimal("0")
        if self.payment_status not in self.OWING_STATUSES:
            return Decimal("0")
        owed = self.total_price - self.amount_paid - self.refunded_amount
        return owed if owed > Decimal("0") else Decimal("0")

    @property
    def cost_total(self) -> Decimal:
        """Себестоимость проданного по этому чеку.

        Сумма снимков закупочной стоимости строк, зафиксированных в момент
        списания со склада (для рулонных — по FIFO-партиям). Возвращённые строки
        не считаем: материал вернулся на склад, себестоимости в заказе больше нет.
        """
        return sum(
            (item.cost_total for item in self.items.all() if not item.is_returned),
            Decimal("0"),
        )

    @property
    def margin(self) -> Decimal:
        """Сколько осталось от заказа после себестоимости материала.

        Та же «валовая маржа», что в Финансах, но по одному чеку: до аренды,
        зарплат и прочих расходов. Возвраты вычитаем — за них деньги отданы.
        """
        return self.total_price - self.refunded_amount - self.cost_total

    def __str__(self) -> str:
        label = f"№{self.order_number}" if self.order_number else str(self.id)
        return f"Чек {label} — {self.total_price}"


class TransactionItem(models.Model):
    """A single line within a receipt: a material sale or a service."""

    class Type(models.TextChoices):
        MATERIAL = "MATERIAL", _("Продажа материала")
        SERVICE = "SERVICE", _("Оказание услуги")

    class SaleMode(models.TextChoices):
        SQM = "SQM", _("По площади / кв.м")
        PIECE = "PIECE", _("Целиком (лист/рулон)")

    receipt = models.ForeignKey(
        Receipt, on_delete=models.CASCADE, related_name="items"
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    material = models.ForeignKey(
        "warehouse.Material",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transaction_items",
    )
    service = models.ForeignKey(
        "services.PrintingService",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transaction_items",
    )
    quantity = models.DecimalField(
        _("количество"), max_digits=12, decimal_places=3, default=Decimal("0")
    )
    price_per_item = models.DecimalField(
        _("цена за единицу на момент продажи"),
        max_digits=12,
        decimal_places=2,
        default=Decimal("0"),
    )
    # How a MATERIAL line was sold: whole piece vs by area (drives stock deduction).
    sale_mode = models.CharField(
        max_length=10, choices=SaleMode.choices, default=SaleMode.SQM, blank=True
    )
    # Себестоимость этой строки, зафиксированная В МОМЕНТ списания со склада
    # (для рулонных — по FIFO-партиям, откуда реально ушёл материал). Снимок,
    # как и price_per_item: последующая переоценка закупки не должна менять
    # прибыль уже закрытых заказов.
    cost_total = models.DecimalField(
        _("себестоимость строки"), max_digits=14, decimal_places=2, default=Decimal("0")
    )
    # Cutting-specific: dimensions of the cut. `letter_type` kept for history only.
    letter_type = models.CharField(max_length=20, blank=True)  # legacy, unused
    width = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    length = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    is_returned = models.BooleanField(_("возвращено"), default=False)

    class Meta:
        verbose_name = _("позиция чека")
        verbose_name_plural = _("позиции чека")

    @property
    def line_total(self) -> Decimal:
        if self.is_returned:
            return Decimal("0")
        # Цену строки округляем ВВЕРХ до целого сома (решение заказчика) — без
        # копеек. Итог чека = сумма таких целых строк, поэтому тоже целый.
        return (self.quantity * self.price_per_item).quantize(Decimal("1"), rounding=ROUND_CEILING)


class Payment(models.Model):
    """Принятая оплата долга — отдельной записью, а не только суммой в
    ``Receipt.amount_paid``.

    Нужна из-за того, как заказчик собирает деньги: клиент приходит и гасит
    сразу несколько заказов одной суммой («общая выплата»), а провести её часто
    нужно задним числом — деньги взяли в понедельник, до компьютера дошли в
    четверг. Одно поле «оплачено» на чеке этого не помнит: на вопрос «когда и
    сколько он реально принёс» отвечать нечем, а общая выплата вообще
    рассыпается на несколько независимых правок.

    Запись справочная: выручка, как и раньше, относится к ДАТЕ ЗАКАЗА, а не к
    дате оплаты (решение заказчика — см. блок «Материалы» в Финансах). Дата
    оплаты здесь нужна ему самому: сверить, кто когда рассчитался.
    """

    receipt = models.ForeignKey(
        Receipt, on_delete=models.CASCADE, related_name="payments"
    )
    amount = models.DecimalField(_("сумма"), max_digits=14, decimal_places=2)
    method = models.CharField(
        _("способ оплаты"),
        max_length=20,
        choices=Receipt.PaymentMethod.choices,
        default=Receipt.PaymentMethod.CASH,
    )
    # Дата, которой деньги считаются принятыми. Может быть в прошлом.
    paid_on = models.DateField(_("дата оплаты"), default=timezone.localdate)
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_taken",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("оплата")
        verbose_name_plural = _("оплаты")
        ordering = ["-paid_on", "-created_at"]

    def __str__(self) -> str:
        return f"Оплата {self.amount} сом по чеку №{self.receipt.order_number}"

    def __str__(self) -> str:
        target = self.material or self.service
        return f"{self.get_type_display()}: {target} × {self.quantity}"
