"""Склейка двух карточек, за которыми стоит один человек.

Появилось из-за того, как копились двойники: касса опознавала клиента по СТРОКЕ
телефона, и `0555 111 222` заводил вторую карточку поверх `+996555111222`.
Заказы и долг одного человека расходились по двум стопкам. Новые дубли
`clients/phones.py` больше не пускает, а вот накопленные нужно свести руками —
какая карточка останется, решает владелец, угадывать это нельзя.

Операция необратимая: вторая карточка удаляется. Поэтому здесь всё в одной
транзакции и с переносом ВСЕГО, что на неё ссылалось, — иначе `on_delete`
утащил бы за собой заявки на рефералов (`new_referred_by` стоит на CASCADE).
"""
from django.db import transaction
from django.utils import timezone

from .models import (
    BalanceOffset, Client, ClientAdvance, OpeningBalance, ReferralBonus, ReferralChangeRequest,
)


class MergeRejected(Exception):
    """Склеить нельзя. Текст уходит пользователю как есть."""


def adopted_referrer(keep: Client, drop: Client):
    """Кого остающаяся карточка получит в рефереры после склейки (или None)."""
    if keep.referred_by_id in (None, drop.pk) and drop.referred_by_id not in (None, keep.pk):
        return drop.referred_by
    return keep.referred_by if keep.referred_by_id not in (None, drop.pk) else None


def would_make_ring(keep: Client, drop: Client) -> bool:
    """Склейка замкнёт цепочку «кто кого привёл»? Та же проверка, что в карточке
    (`ClientSerializer.validate_referred_by`): идём вверх от нового реферера, и
    если встретили остающуюся или удаляемую карточку (после склейки это одна) —
    кольцо. Ограничитель — от битых данных."""
    node, hops = adopted_referrer(keep, drop), 0
    while node is not None and hops < 50:
        if node.pk in (keep.pk, drop.pk):
            return True
        node, hops = node.referred_by, hops + 1
    return False


def merge_summary(keep: Client, drop: Client) -> dict:
    """Что переедет при склейке. Показывается ДО подтверждения: удаление
    карточки необратимо, и человек должен видеть объём заранее."""
    from .serializers import client_debt

    return {
        "orders": drop.receipts.count(),
        # Та же функция долга, что у карточки: чеки + входящий долг (волна 2).
        "debt": client_debt(drop),
        "referrals": drop.referrals.count(),
        "advance": sum((a.remaining for a in drop.advances.filter(reverted_at__isnull=True)), 0),
        "ring": would_make_ring(keep, drop),
        "keep": keep.display_name,
        "drop": drop.display_name,
        "drop_phone": drop.phone,
    }


@transaction.atomic
def merge_clients(keep: Client, drop: Client, *, user=None) -> Client:
    """Перенести всё с `drop` на `keep` и удалить `drop`. Возвращает `keep`."""
    if keep.pk == drop.pk:
        raise MergeRejected("Нельзя объединить карточку с ней же.")
    if would_make_ring(keep, drop):
        raise MergeRejected(
            f"Нельзя объединить: «{drop.display_name}» приведён клиентом из ветки "
            f"«{keep.display_name}» — получится кольцо в рефералах. Сначала смените "
            "реферера у одной из карточек."
        )

    # Заказы — главное, ради чего всё затевалось. FK стоит на PROTECT, так что
    # без переноса удаление всё равно не прошло бы.
    drop.receipts.update(client=keep)
    # Авансы и зачёты — деньги клиента, они едут вместе с заказами. PROTECT на
    # авансе не даст удалить карточку, пока он на ней висит.
    ClientAdvance.objects.filter(client=drop).update(client=keep)
    BalanceOffset.objects.filter(client=drop).update(client=keep)
    # Входящие остатки на дату переезда (волна 2) — тоже деньги клиента.
    OpeningBalance.objects.filter(client=drop).update(client=keep)
    # Договорные цены (волна 2): переезжают те, которых у остающейся карточки
    # нет; совпавшие остаются её (она главная), дубль удаляется с карточкой.
    from .models import ClientPrice

    have = set(ClientPrice.objects.filter(client=keep).values_list("service_id", "material_id", "sale_mode"))
    for cp in ClientPrice.objects.filter(client=drop):
        if (cp.service_id, cp.material_id, cp.sale_mode) not in have:
            cp.client = keep
            cp.save(update_fields=["client"])

    # Начисления реферального бонуса едут вместе с карточкой. У приведённого
    # действующее начисление одно (уникально): если оно было у обеих карточек,
    # остаётся выплаченное (или остающейся), второе снимается.
    ReferralBonus.objects.filter(referrer=drop).update(referrer=keep)
    now = timezone.now()
    keep_active = ReferralBonus.objects.filter(referred=keep, voided_at__isnull=True).first()
    for row in ReferralBonus.objects.filter(referred=drop):
        row.referred = keep
        if row.voided_at is None and keep_active is not None:
            if row.paid_amount > 0 and keep_active.paid_amount == 0:
                keep_active.voided_at = now
                keep_active.save(update_fields=["voided_at"])
                keep_active = row
            else:
                row.voided_at = now
        elif row.voided_at is None:
            keep_active = row
        row.save()
    # Привёл сам себя после склейки — это не начисление.
    ReferralBonus.objects.filter(referrer=keep, referred=keep, voided_at__isnull=True).update(voided_at=now)

    # Рефералы: те, кого привела удаляемая карточка, переезжают на остающуюся.
    Client.objects.filter(referred_by=drop).update(referred_by=keep)

    # Заявки на смену реферера — во всех трёх ролях, включая CASCADE-ссылку:
    # оставь её на удаляемой карточке, и заявки исчезнут вместе с ней.
    ReferralChangeRequest.objects.filter(client=drop).update(client=keep)
    ReferralChangeRequest.objects.filter(new_referred_by=drop).update(new_referred_by=keep)
    ReferralChangeRequest.objects.filter(previous_referred_by=drop).update(previous_referred_by=keep)
    # После переноса заявка могла начать предлагать клиента самому себе.
    ReferralChangeRequest.objects.filter(client=keep, new_referred_by=keep).update(
        new_referred_by=None
    )

    # Пустые поля остающейся карточки дозаполняем из удаляемой: телефон и имя
    # там свои, а вот привязка к Telegram или выданный пароль могли достаться
    # только одной из двух — терять их не за что.
    fill = []
    if not keep.telegram_chat_id and drop.telegram_chat_id:
        keep.telegram_chat_id = drop.telegram_chat_id
        fill.append("telegram_chat_id")
    if not keep.portal_password and drop.portal_password:
        keep.portal_password = drop.portal_password
        fill.append("portal_password")
    if not keep.full_name and drop.full_name:
        keep.full_name = drop.full_name
        fill.append("full_name")
    if not keep.company_name and drop.company_name:
        keep.company_name = drop.company_name
        fill.append("company_name")

    # Реферер: берём с удаляемой, если у остающейся его не было. Ссылку на саму
    # себя не допускаем — иначе клиент оказался бы приведён самим собой.
    if keep.referred_by_id in (None, drop.pk) and drop.referred_by_id not in (None, keep.pk):
        keep.referred_by_id = drop.referred_by_id
        fill.append("referred_by")
    elif keep.referred_by_id == drop.pk:
        keep.referred_by_id = None
        fill.append("referred_by")

    if fill:
        keep.save(update_fields=fill)

    drop.delete()
    keep.refresh_from_db()
    return keep
