"""Привязка Telegram-чата к клиенту только по СВОЕМУ контакту (CLI-09, D-97).

Раньше вебхук и polling-бот привязывали чат по `contact.phone_number`, не
сверяя `contact.user_id` с отправителем: чужой человек присылал карточку
контакта настоящего клиента, и чеки с суммами начинали уходить ему. Номер
искали по хвосту СЫРОЙ строки, поэтому «0700 11 22 33» бот не находил вовсе.
"""
from django.test import override_settings
from rest_framework.test import APITestCase

from clients.models import Client
from integrations.linking import link_chat

URL = "/api/integrations/telegram/customer/webhook/"
SECRET = {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "s3cret"}


def update(*, chat=999111, sender=999111, phone="+996555112233", contact_user=999111):
    contact = {"phone_number": phone}
    if contact_user is not None:
        contact["user_id"] = contact_user
    message = {"chat": {"id": chat}, "contact": contact}
    if sender is not None:
        message["from"] = {"id": sender}
    return {"message": message}


@override_settings(TELEGRAM_WEBHOOK_SECRET="s3cret")
class WebhookOwnershipTests(APITestCase):
    def setUp(self):
        self.customer = Client.objects.create(
            full_name="Настоящий клиент", phone="+996555112233", telegram_chat_id="777000",
        )

    def post(self, body):
        return self.client.post(URL, body, format="json", **SECRET)

    def test_foreign_contact_card_is_refused(self):
        """Чужой чат прислал карточку номера клиента: user_id контакта — 777000."""
        r = self.post(update(chat=999111, sender=999111, contact_user=777000))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "not_own_contact")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.telegram_chat_id, "777000")      # настоящий не потерян

    def test_contact_without_user_id_is_refused(self):
        r = self.post(update(contact_user=None))
        self.assertEqual(r.data["status"], "not_own_contact")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.telegram_chat_id, "777000")

    def test_message_without_sender_is_refused(self):
        r = self.post(update(sender=None))
        self.assertEqual(r.data["status"], "not_own_contact")

    def test_own_contact_links(self):
        r = self.post(update(chat=555, sender=555, contact_user=555))
        self.assertEqual(r.data["status"], "linked")
        self.assertEqual(r.data["client"], self.customer.id)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.telegram_chat_id, "555")

    def test_number_is_found_in_any_spelling(self):
        spelled = Client.objects.create(full_name="Пишется иначе", phone="0700 11 22 33")
        r = self.post(update(chat=42, sender=42, contact_user=42, phone="+996700112233"))
        self.assertEqual(r.data["status"], "linked", r.data)
        spelled.refresh_from_db()
        self.assertEqual(spelled.telegram_chat_id, "42")

    def test_unknown_number(self):
        r = self.post(update(phone="+996111000999"))
        self.assertEqual(r.data["status"], "not_found")

    def test_short_junk_phone_does_not_match_everything(self):
        r = self.post(update(phone="12"))
        self.assertEqual(r.data["status"], "not_found")


class LinkHelperTests(APITestCase):
    """Тот же помощник зовёт и polling-бот (`run_customer_bot`)."""

    def test_helper_contract(self):
        c = Client.objects.create(full_name="Бот", phone="0555 11 22 33")
        self.assertEqual(link_chat(phone="+996555112233", contact_user_id=5, sender_id=6, chat_id=6)[0], "not_own_contact")
        status, client = link_chat(phone="+996555112233", contact_user_id=6, sender_id=6, chat_id=6)
        self.assertEqual((status, client.pk), ("linked", c.pk))
        c.refresh_from_db()
        self.assertEqual(c.telegram_chat_id, "6")
        # строка и число — один и тот же идентификатор
        self.assertEqual(link_chat(phone="555112233", contact_user_id="6", sender_id=6, chat_id=6)[0], "linked")
