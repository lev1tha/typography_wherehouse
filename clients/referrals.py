"""Реферальный бонус: таблица начислений (CLI-07, D-95).

Правило владельца: платим за приведённого клиента, у которого есть ОПЛАЧЕННЫЙ и
не возвращённый заказ — один раз, по ставке на момент начисления. Смена ставки
прошлое не меняет. Выплата — запись «выплачено столько-то такого-то числа»; в
расходы бонус не списывается (решение заказчика: бонус справочный).

Откуда берутся начисления:
  * новые — сигналом на сохранение чека (`clients.signals`): оплатили заказ
    приведённого клиента — строка `ReferralBonus` со ставкой этой минуты;
  * старые привязки (до таблицы) — расчётом при показе карточки: «по текущей
    ставке, ещё не зафиксировано» (`estimated`). Данные миграцией не трогаем.
    Перед сменой ставки (`pre_save` настроек финансов) такие строки
    фиксируются по СТАРОЙ ставке — иначе смена ставки переписала бы их;
  * выплата «виртуальной» строки сначала фиксирует её.

Не считается оплаченным: заказ в долге, возвращённый (хоть частично), отменённый,
неподтверждённый онлайн-счёт, закрытый списанием долга (клиент не платил).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Client, ReferralBonus

ZERO = Decimal("0")


class BonusRejected(Exception):
    """Операцию с бонусом нельзя провести. Текст уходит пользователю как есть."""


def current_rate() -> Decimal:
    from finance.models import FinanceSettings

    return FinanceSettings.load().referral_bonus or ZERO


def _written_off_receipts():
    """id чеков, у которых долг списан — ЧИСТАЯ сумма списаний больше нуля.

    Отменённое списание остаётся записью своим днём, а отмена — встречной
    записью с минусом (D-158): заказ, у которого списание отменили и который
    потом оплатили, — снова оплаченный, а не «закрытый списанием»."""
    from django.db.models import Sum

    from sales.models import Payment

    return (
        Payment.objects.filter(method=Payment.Method.WRITE_OFF)
        .values("receipt_id").annotate(net=Sum("amount")).filter(net__gt=0)
        .values("receipt_id")
    )


def qualifying_receipt(referred: Client):
    """Первый оплаченный и не возвращённый заказ клиента — или None."""
    from sales.models import Receipt

    return (
        Receipt.objects.filter(
            client=referred, payment_status=Receipt.PaymentStatus.PAID,
            status=Receipt.Status.COMPLETED, refunded_amount=0,
            revenue_recognized_at__isnull=False, total_price__gt=0,
        )
        .exclude(pk__in=_written_off_receipts())
        .order_by("revenue_recognized_at", "pk")
        .first()
    )


def accrued_day(receipt) -> date:
    """День начисления: когда заказ стал оплаченным — дата последней оплаты
    долга, а если платили сразу, то день заказа."""
    day = timezone.localtime(receipt.revenue_recognized_at).date()
    last = receipt.payments.order_by("-paid_on").values_list("paid_on", flat=True).first()
    return max(day, last) if last else day


def _create_row(referred: Client, receipt, rate: Decimal, *, day: date | None = None) -> ReferralBonus:
    """Новое начисление. День — когда система узнала, что заказ оплачен (сегодня),
    а для зафиксированных старых привязок — историческая дата оплаты."""
    return ReferralBonus.objects.create(
        referrer=referred.referred_by, referred=referred, receipt=receipt,
        order_number=receipt.order_number, amount=rate,
        accrued_on=day or timezone.localdate(),
    )


def active_row(referred: Client):
    return ReferralBonus.objects.filter(referred=referred, voided_at__isnull=True).first()


@transaction.atomic
def refresh_bonus(referred: Client, *, rate: Decimal | None = None):
    """Привести начисление клиента в соответствие с его заказами.

    Начисление, у которого заказ перестал быть оплаченным (возврат, откат
    оплаты, списание) и по которому ещё не платили, снимается. Нет действующей
    строки, а оплаченный заказ есть — создаётся новая по ставке `rate` (по
    умолчанию — текущей). Ставка 0 ничего не начисляет: пока программа выключена,
    повода для бонуса нет.
    """
    if referred.referred_by_id is None:
        return None
    row = active_row(referred)
    receipt = qualifying_receipt(referred)
    if row is not None:
        if receipt_still_ok(row):
            return row
        if row.paid_amount > 0:
            return row           # деньги выплачены — запись остаётся как есть
        row.voided_at = timezone.now()
        row.save(update_fields=["voided_at"])
        row = None
    rate = current_rate() if rate is None else rate
    if receipt is None or rate <= 0:
        return None
    return _create_row(referred, receipt, rate)


def receipt_still_ok(row) -> bool:
    from sales.models import Receipt

    r = row.receipt
    return bool(
        r is not None
        and r.payment_status == Receipt.PaymentStatus.PAID
        and r.status == Receipt.Status.COMPLETED
        and r.refunded_amount == 0
        and not _written_off_receipts().filter(receipt_id=r.pk).exists()
    )


def on_referrer_changed(referred: Client) -> None:
    """Реферера сменили: невыплаченное начисление старого реферера снимается,
    новому — считается заново."""
    for row in ReferralBonus.objects.filter(referred=referred, voided_at__isnull=True, paid_amount=0):
        row.voided_at = timezone.now()
        row.save(update_fields=["voided_at"])
    refresh_bonus(referred)


def freeze_all(rate: Decimal) -> int:
    """Зафиксировать «расчётные» начисления старых привязок по ставке `rate`
    (вызывается перед сменой ставки). Возвращает, сколько строк создано."""
    if rate <= 0:
        return 0
    created = 0
    for referred in Client.objects.filter(referred_by__isnull=False).select_related("referred_by"):
        if active_row(referred) is not None:
            continue
        receipt = qualifying_receipt(referred)
        if receipt is not None:
            _create_row(referred, receipt, rate, day=accrued_day(receipt))
            created += 1
    return created


def bonus_state(row: ReferralBonus | None, virtual: dict | None = None) -> dict | None:
    """Описание начисления для карточки. `virtual` — расчётная строка старой привязки."""
    if row is None and virtual is None:
        return None
    if row is not None:
        amount, paid = row.amount, row.paid_amount
        data = {
            "id": row.id, "amount": amount, "accrued_on": row.accrued_on,
            "order_number": row.order_number, "paid_amount": paid, "paid_on": row.paid_on,
            "estimated": False,
        }
    else:
        amount, paid = virtual["amount"], ZERO
        data = {
            "id": None, "amount": amount, "accrued_on": virtual["accrued_on"],
            "order_number": virtual["order_number"], "paid_amount": ZERO, "paid_on": None,
            "estimated": True,
        }
    data["due"] = max(ZERO, amount - paid)
    data["status"] = "paid" if paid >= amount else ("partial" if paid > 0 else "accrued")
    return data


def referral_summary(referrer: Client, ltv_of) -> dict:
    """Блок «рефералы» карточки: кого привёл, начислено / выплачено / к выплате."""
    rate = current_rate()
    referred = list(referrer.referrals.all())
    rows = {
        r.referred_id: r
        for r in ReferralBonus.objects.filter(referred__in=referred, voided_at__isnull=True)
    }
    items, total_ltv = [], ZERO
    accrued = paid = ZERO
    for ref in referred:
        ltv = ltv_of(ref)
        total_ltv += ltv
        row = rows.get(ref.pk)
        virtual = None
        if row is None and rate > 0:
            receipt = qualifying_receipt(ref)
            if receipt is not None:
                virtual = {"amount": rate, "accrued_on": accrued_day(receipt), "order_number": receipt.order_number}
        state = bonus_state(row, virtual)
        if state:
            accrued += state["amount"]
            paid += state["paid_amount"]
        items.append({"id": ref.id, "display_name": ref.display_name, "lifetime_value": ltv, "bonus": state})
    return {
        "count": len(items),
        "total_value": total_ltv,
        # «bonus» — прежнее имя поля: теперь это начислено (раньше ставка × число привязок)
        "bonus": accrued,
        "bonus_accrued": accrued,
        "bonus_paid": paid,
        "bonus_due": max(ZERO, accrued - paid),
        "rate": rate,
        "list": items,
    }


@transaction.atomic
def pay_bonus(referrer: Client, referred: Client, *, amount=None, paid_on: date | None = None, user=None):
    """Записать выплату бонуса за приведённого клиента (всю или часть)."""
    if referred.referred_by_id != referrer.pk:
        raise BonusRejected("Этого клиента привёл не он.")
    row = active_row(referred)
    if row is None:
        row = refresh_bonus(referred)
        if row is None:
            raise BonusRejected("Бонус за этого клиента не начислен: нет оплаченного заказа или ставка 0.")
    row = ReferralBonus.objects.select_for_update().get(pk=row.pk)
    if row.referrer_id != referrer.pk:
        raise BonusRejected("Бонус за этого клиента начислен другому клиенту.")
    due = row.amount - row.paid_amount
    if due <= 0:
        raise BonusRejected("Бонус уже выплачен полностью.")
    if amount is None or amount == "":
        value = due
    else:
        try:
            value = Decimal(str(amount))
        except Exception:
            raise BonusRejected("Некорректная сумма.")
        if not value.is_finite() or value <= 0:
            raise BonusRejected("Сумма должна быть больше нуля.")
    if value > due:
        raise BonusRejected(f"К выплате осталось {due} сом — больше записать нельзя.")
    day = paid_on or timezone.localdate()
    if day > timezone.localdate():
        raise BonusRejected("Дата выплаты не может быть в будущем.")
    row.paid_amount += value
    row.paid_on = day
    row.paid_by = user
    row.save(update_fields=["paid_amount", "paid_on", "paid_by"])
    return row, value


@transaction.atomic
def unpay_bonus(referrer: Client, referred: Client):
    """Снять запись о выплате (ошибочно отметили)."""
    row = active_row(referred)
    if row is None or row.referrer_id != referrer.pk or row.paid_amount <= 0:
        raise BonusRejected("Выплат по этому клиенту нет.")
    row.paid_amount = ZERO
    row.paid_on = None
    row.paid_by = None
    row.save(update_fields=["paid_amount", "paid_on", "paid_by"])
    return row
