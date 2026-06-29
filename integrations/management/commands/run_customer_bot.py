"""Run the customer Telegram bot (@Cloude_Customer_Bot) via long polling.

Handles phone verification (Share Contact), account linking against the Client
model, and the in-bot menu «Мои заказы» / «Мои чеки» backed by the ORM.

Usage:
    TELEGRAM_CUSTOMER_BOT_TOKEN=... python manage.py run_customer_bot
"""
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from clients.models import Client
from sales.models import Receipt

MENU_ORDERS = "🧾 Мои заказы"
MENU_RECEIPTS = "💳 Мои чеки"


@sync_to_async
def link_client_by_phone(phone: str, chat_id: int):
    digits = phone.lstrip("+")
    client = (
        Client.objects.filter(phone__endswith=digits[-9:]).first()
        if len(digits) >= 9
        else None
    )
    if client:
        client.telegram_chat_id = str(chat_id)
        client.save(update_fields=["telegram_chat_id"])
    return client


@sync_to_async
def client_for_chat(chat_id: int):
    return Client.objects.filter(telegram_chat_id=str(chat_id)).first()


@sync_to_async
def recent_receipts_text(client) -> str:
    receipts = list(
        Receipt.objects.filter(client=client).order_by("-created_at")[:10]
    )
    if not receipts:
        return "У вас пока нет чеков."
    lines = []
    for r in receipts:
        lines.append(
            f"№ {str(r.id)[:8]} — {r.total_price} сом — "
            f"{r.get_payment_status_display()} ({r.created_at:%d.%m.%Y})"
        )
    return "\n".join(lines)


class Command(BaseCommand):
    help = "Запускает клиентский Telegram-бот (long polling)."

    def handle(self, *args, **options):
        token = settings.TELEGRAM_CUSTOMER_BOT_TOKEN
        if not token:
            raise CommandError(
                "TELEGRAM_CUSTOMER_BOT_TOKEN не задан — укажите токен в .env."
            )

        from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
        from telegram.ext import (
            Application,
            CommandHandler,
            ContextTypes,
            MessageHandler,
            filters,
        )

        contact_kb = ReplyKeyboardMarkup(
            [
                [KeyboardButton("📱 Поделиться контактом", request_contact=True)],
                [KeyboardButton(MENU_ORDERS), KeyboardButton(MENU_RECEIPTS)],
            ],
            resize_keyboard=True,
        )

        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await update.message.reply_text(
                "Добро пожаловать в Cloude! Чтобы привязать аккаунт, "
                "поделитесь контактом 👇",
                reply_markup=contact_kb,
            )

        async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
            contact = update.message.contact
            client = await link_client_by_phone(
                contact.phone_number, update.effective_chat.id
            )
            if client:
                await update.message.reply_text(
                    "✅ Аккаунт привязан! Теперь вы будете получать чеки и "
                    "уведомления о заказах.",
                    reply_markup=contact_kb,
                )
            else:
                await update.message.reply_text(
                    "Не нашли вас в базе по этому номеру. Обратитесь к складовщику."
                )

        async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
            client = await client_for_chat(update.effective_chat.id)
            if not client:
                await update.message.reply_text(
                    "Сначала привяжите аккаунт через «Поделиться контактом».",
                    reply_markup=contact_kb,
                )
                return
            text = await recent_receipts_text(client)
            await update.message.reply_text(text, reply_markup=contact_kb)

        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.CONTACT, on_contact))
        app.add_handler(
            MessageHandler(
                filters.Regex(f"^({MENU_ORDERS}|{MENU_RECEIPTS})$"), on_menu
            )
        )

        self.stdout.write(self.style.SUCCESS("Клиентский бот запущен (polling). Ctrl+C для остановки."))
        app.run_polling()
