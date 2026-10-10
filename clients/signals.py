"""Сигналы клиентов: реферальные начисления (CLI-07, D-95).

Бонус начисляется в момент, когда заказ приведённого клиента стал оплаченным, —
поэтому ловим сохранение чека, а не пересчитываем при показе (иначе ставка
«на момент начисления» была бы ставкой на момент просмотра). Любой сбой здесь
не должен ронять продажу: касса — главное, бонус — учётная надстройка.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from finance.models import FinanceSettings
from sales.models import Payment, Receipt

from .models import Client, ReferralBonus

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Receipt, dispatch_uid="clients_referral_on_receipt")
def referral_on_receipt(sender, instance, created, **kwargs):
    if not instance.client_id or kwargs.get("raw"):
        return
    # Дёшево отсекаем сохранения, которые бонуса не касаются.
    relevant = (
        instance.payment_status == Receipt.PaymentStatus.PAID
        or instance.refunded_amount > 0
        or instance.status == Receipt.Status.CANCELLED
        or ReferralBonus.objects.filter(receipt=instance, voided_at__isnull=True).exists()
    )
    if not relevant:
        return
    try:
        from .referrals import refresh_bonus

        with transaction.atomic():
            client = Client.objects.filter(pk=instance.client_id, referred_by__isnull=False).first()
            if client is not None:
                refresh_bonus(client)
    except Exception:       # noqa: BLE001 — бонус не должен ронять продажу
        logger.exception("Не удалось обновить реферальный бонус клиента %s", instance.client_id)


@receiver(post_save, sender=Payment, dispatch_uid="clients_referral_on_write_off")
def referral_on_write_off(sender, instance, created, **kwargs):
    """Заказ, закрытый списанием долга, — не оплаченный: клиент не платил.
    Запись списания появляется уже после сохранения чека, поэтому снимаем
    начисление здесь."""
    if kwargs.get("raw") or not created or instance.method != Payment.Method.WRITE_OFF:
        return
    try:
        from .referrals import refresh_bonus

        with transaction.atomic():
            client = Client.objects.filter(
                pk=instance.receipt.client_id, referred_by__isnull=False
            ).first()
            if client is not None:
                refresh_bonus(client)
    except Exception:       # noqa: BLE001
        logger.exception("Не удалось обновить реферальный бонус после списания долга")


@receiver(pre_save, sender=Client, dispatch_uid="clients_referrer_before")
def remember_referrer(sender, instance, **kwargs):
    if instance.pk and not kwargs.get("raw"):
        instance._old_referred_by_id = (
            Client.objects.filter(pk=instance.pk).values_list("referred_by_id", flat=True).first()
        )


@receiver(post_save, sender=Client, dispatch_uid="clients_referrer_after")
def referrer_changed(sender, instance, created, **kwargs):
    if created or kwargs.get("raw"):
        return
    old = getattr(instance, "_old_referred_by_id", None)
    if old == instance.referred_by_id:
        return
    try:
        from .referrals import on_referrer_changed

        with transaction.atomic():
            on_referrer_changed(instance)
    except Exception:       # noqa: BLE001
        logger.exception("Не удалось пересчитать бонус после смены реферера клиента %s", instance.pk)


@receiver(pre_save, sender=FinanceSettings, dispatch_uid="clients_freeze_bonus_before_rate_change")
def freeze_before_rate_change(sender, instance, **kwargs):
    """Смена ставки не переписывает прошлое: расчётные начисления старых
    привязок фиксируются по ещё старой ставке."""
    if kwargs.get("raw") or not instance.pk:
        return
    old = FinanceSettings.objects.filter(pk=instance.pk).values_list("referral_bonus", flat=True).first()
    if old is not None and old != instance.referral_bonus and old > 0:
        from .referrals import freeze_all

        freeze_all(old)
