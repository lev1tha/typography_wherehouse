import uuid
from decimal import ROUND_CEILING, Decimal

from django.db import models
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
    stock_deducted = models.BooleanField(
        _("склад списан"),
        default=False,
        help_text=_("Материал уже списан со склада — для корректного возврата."),
    )
    # Online payment gateway reference / link, filled when method is ONLINE.
    payment_reference = models.CharField(max_length=255, blank=True)
    payment_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
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
        """Остаток к оплате: total − предоплата − возвраты. 0, если оплачен/отменён."""
        if self.status == self.Status.CANCELLED:
            return Decimal("0")
        if self.payment_status != self.PaymentStatus.PENDING:
            return Decimal("0")
        owed = self.total_price - self.amount_paid - self.refunded_amount
        return owed if owed > Decimal("0") else Decimal("0")

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

    def __str__(self) -> str:
        target = self.material or self.service
        return f"{self.get_type_display()}: {target} × {self.quantity}"
