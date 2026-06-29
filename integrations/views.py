import hashlib
import logging
import uuid

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from clients.models import Client
from integrations.payments import get_gateway
from integrations.telegram import send_customer_receipt
from sales.models import Receipt
from sales.sale_service import confirm_payment

logger = logging.getLogger(__name__)


def _lines_text(receipt: Receipt) -> str:
    lines = []
    for item in receipt.items.all():
        target = item.material.name if item.material_id else item.service.name
        lines.append(f"• {target} × {item.quantity} = {item.line_total} сом")
    return "\n".join(lines)


class PaymentWebhookView(APIView):
    """Public endpoint hit by the payment gateway when a payment completes.

    Validates the callback through the configured gateway, then settles the
    receipt (PENDING → PAID + stock deduction) and pushes the receipt to the
    customer's Telegram.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        result = get_gateway().verify_webhook(request)
        if not result.get("paid"):
            return self._respond(request, ok=False, message="not paid")
        reference = result.get("reference")
        receipt = Receipt.objects.filter(payment_reference=reference).first()
        if not receipt:
            return self._respond(request, ok=False, message="unknown receipt")
        confirm_payment(receipt)
        if receipt.client:
            send_customer_receipt(receipt.client, receipt, _lines_text(receipt))
        return self._respond(request, ok=True, message="ok")

    def _respond(self, request, *, ok: bool, message: str):
        """FreedomPay expects a signed XML ack; other providers get JSON."""
        if (settings.PAYMENT_GATEWAY or "").lower() != "freedompay":
            code = status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST
            return Response({"status": message}, status=code)
        salt = uuid.uuid4().hex
        status_str = "ok" if ok else "error"
        # Sign: md5(script_name;pg_salt;pg_status;secret) — values sorted by key.
        raw = ";".join(["webhook", status_str, salt, settings.PAYMENT_API_SECRET])
        sig = hashlib.md5(raw.encode("utf-8")).hexdigest()
        xml = (
            f"<?xml version='1.0' encoding='utf-8'?><response>"
            f"<pg_status>{status_str}</pg_status>"
            f"<pg_description>{message}</pg_description>"
            f"<pg_salt>{salt}</pg_salt><pg_sig>{sig}</pg_sig></response>"
        )
        return HttpResponse(xml, content_type="application/xml")


class MockPayView(APIView):
    """Dev-only helper: simulate a successful online payment for a receipt."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, receipt_id):
        receipt = get_object_or_404(Receipt, pk=receipt_id)
        confirm_payment(receipt)
        if receipt.client:
            send_customer_receipt(receipt.client, receipt, _lines_text(receipt))
        return Response({"status": "paid", "receipt": str(receipt.id)})


class TelegramCustomerWebhookView(APIView):
    """Customer bot webhook. On a shared contact, link telegram_chat_id to the
    matching Client by phone number.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        update = request.data or {}
        message = update.get("message", {})
        contact = message.get("contact")
        chat_id = (message.get("chat") or {}).get("id")
        if not contact or not chat_id:
            return Response({"status": "ignored"})

        phone = (contact.get("phone_number") or "").lstrip("+")
        # Match by the trailing digits to tolerate +996 / 996 / 0 prefixes.
        client = (
            Client.objects.filter(phone__endswith=phone[-9:]).first()
            if len(phone) >= 9
            else None
        )
        if client:
            client.telegram_chat_id = str(chat_id)
            client.save(update_fields=["telegram_chat_id"])
            return Response({"status": "linked", "client": client.id})
        return Response({"status": "not_found"})
