"""Привязка Telegram-чата к карточке клиента (CLI-09, D-97).

Единственное место, где чат становится «чатом клиента»: его зовут и вебхук, и
polling-бот. Правило одно: **клиент делится СВОИМ контактом**. У карточки
контакта в Telegram есть `user_id` — аккаунт, которому номер принадлежит; он
обязан совпасть с аккаунтом отправителя (`message.from.id`). Иначе любой, кто
знает номер клиента (а номера клиентов — не секрет), пересылал бы боту чужую
карточку и получал его чеки с суммами.

Клиента ищем по цифрам номера (`phones.find_client_by_phone`), а не по хвосту
сырой строки: «0700 11 22 33» и «+996700112233» — один человек.
"""
from __future__ import annotations

from clients.models import Client
from clients.phones import find_client_by_phone

LINKED = "linked"
NOT_FOUND = "not_found"
NOT_OWN = "not_own_contact"


def link_chat(*, phone, contact_user_id, sender_id, chat_id) -> tuple[str, Client | None]:
    """Привязать `chat_id` к клиенту с номером `phone`. Возвращает (статус, клиент)."""
    if contact_user_id in (None, "") or sender_id in (None, "") or str(contact_user_id) != str(sender_id):
        return NOT_OWN, None
    client = find_client_by_phone(str(phone or "").strip())
    if client is None:
        return NOT_FOUND, None
    client.telegram_chat_id = str(chat_id)
    client.save(update_fields=["telegram_chat_id"])
    return LINKED, client
