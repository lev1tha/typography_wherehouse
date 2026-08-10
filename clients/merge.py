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

from .models import Client, ReferralChangeRequest


class MergeRejected(Exception):
    """Склеить нельзя. Текст уходит пользователю как есть."""


def merge_summary(keep: Client, drop: Client) -> dict:
    """Что переедет при склейке. Показывается ДО подтверждения: удаление
    карточки необратимо, и человек должен видеть объём заранее."""
    return {
        "orders": drop.receipts.count(),
        "debt": sum((r.debt for r in drop.receipts.all()), 0),
        "referrals": drop.referrals.count(),
        "keep": keep.display_name,
        "drop": drop.display_name,
        "drop_phone": drop.phone,
    }


@transaction.atomic
def merge_clients(keep: Client, drop: Client, *, user=None) -> Client:
    """Перенести всё с `drop` на `keep` и удалить `drop`. Возвращает `keep`."""
    if keep.pk == drop.pk:
        raise MergeRejected("Нельзя объединить карточку с ней же.")

    # Заказы — главное, ради чего всё затевалось. FK стоит на PROTECT, так что
    # без переноса удаление всё равно не прошло бы.
    drop.receipts.update(client=keep)

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
