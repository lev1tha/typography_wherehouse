"""Real Telegram Bot API integration (staff alerts + customer receipts).

Uses the synchronous HTTP API (``https://api.telegram.org/bot<token>/...``)
via ``requests`` so it can be called inline from Django views without an async
event loop. If a bot token is not configured the calls degrade to a logged
no-op, so the system keeps working in development before tokens are provisioned.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 10


def _send_message(token: str, chat_id, text: str, **kwargs) -> bool:
    if not token:
        logger.info("Telegram token missing — skipping message to %s: %s", chat_id, text)
        return False
    url = API_BASE.format(token=token, method="sendMessage")
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", **kwargs}
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram sendMessage failed (chat %s): %s", chat_id, exc)
        return False


def notify_low_stock(material) -> None:
    """Alert the staff chat(s) when a material drops below critical balance."""
    text = (
        f"⚠️ <b>Внимание!</b> Материал «{material.name}» на исходе. "
        f"Осталось всего {material.quantity}. Требуется закупка!"
    )
    token = settings.TELEGRAM_STAFF_BOT_TOKEN
    for chat_id in settings.TELEGRAM_STAFF_CHAT_IDS:
        _send_message(token, chat_id, text)


def send_customer_receipt(client, receipt, lines_text: str) -> bool:
    """Send an electronic receipt to a linked customer's Telegram chat."""
    if not client or not client.telegram_chat_id:
        return False
    text = (
        f"🧾 <b>Ваш чек</b> №{receipt.id}\n\n"
        f"{lines_text}\n\n"
        f"Итого: <b>{receipt.total_price}</b> сом\n"
        f"Оплата: {receipt.get_payment_method_display()} — "
        f"{receipt.get_payment_status_display()}"
    )
    kwargs = {}
    if receipt.payment_url:
        kwargs["reply_markup"] = {
            "inline_keyboard": [[{"text": "Оплатить онлайн", "url": receipt.payment_url}]]
        }
    return _send_message(
        settings.TELEGRAM_CUSTOMER_BOT_TOKEN, client.telegram_chat_id, text, **kwargs
    )


def notify_customer(client, text: str) -> bool:
    """Generic customer notification (order ready, refund confirmation, ...)."""
    if not client or not client.telegram_chat_id:
        return False
    return _send_message(
        settings.TELEGRAM_CUSTOMER_BOT_TOKEN, client.telegram_chat_id, text
    )
