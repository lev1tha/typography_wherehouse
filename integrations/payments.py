"""Payment gateway abstraction.

The concrete provider is selected via the ``PAYMENT_GATEWAY`` setting. Each
provider implements ``create_invoice`` (returns a payment URL/QR reference) and
``verify_webhook`` (validates an incoming payment-confirmation callback).

- ``mock``       — development gateway (deterministic local link).
- ``freedompay`` — real FreedomPay / PayBox integration (the de-facto gateway
  in Kyrgyzstan). Requires PAYMENT_API_KEY (pg_merchant_id) and
  PAYMENT_API_SECRET (the merchant secret key) in the environment.
"""
import hashlib
import logging
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class Invoice:
    reference: str
    payment_url: str


class BasePaymentGateway:
    name = "base"

    def create_invoice(self, receipt) -> Invoice:  # pragma: no cover - interface
        raise NotImplementedError

    def verify_webhook(self, request) -> dict:  # pragma: no cover - interface
        """Validate the callback and return {'reference': str, 'paid': bool}."""
        raise NotImplementedError


class MockGateway(BasePaymentGateway):
    """Development gateway: produces a deterministic local payment link."""

    name = "mock"

    def create_invoice(self, receipt) -> Invoice:
        ref = f"MOCK-{receipt.id}"
        url = f"{settings.SITE_BASE_URL}/api/integrations/payments/mock/{receipt.id}/"
        return Invoice(reference=ref, payment_url=url)

    def verify_webhook(self, request) -> dict:
        reference = request.data.get("reference", "")
        return {"reference": reference, "paid": True}


class FreedomPayGateway(BasePaymentGateway):
    """Real FreedomPay (PayBox) gateway.

    Signature algorithm (PayBox standard): md5 of
    ``script_name;<param-values sorted by key>;secret_key``.
    """

    name = "freedompay"
    INIT_URL = "https://api.freedompay.kg/init_payment.php"
    INIT_SCRIPT = "init_payment.php"
    # Last path segment of our webhook URL — used to verify callback signature.
    RESULT_SCRIPT = "webhook"

    def __init__(self):
        self.merchant_id = settings.PAYMENT_API_KEY
        self.secret = settings.PAYMENT_API_SECRET

    def _sign(self, script_name: str, params: dict) -> str:
        ordered = [str(params[k]) for k in sorted(params)]
        raw = ";".join([script_name, *ordered, self.secret])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def create_invoice(self, receipt) -> Invoice:
        params = {
            "pg_merchant_id": self.merchant_id,
            "pg_order_id": str(receipt.id),
            "pg_amount": str(receipt.total_price),
            "pg_currency": "KGS",
            "pg_description": f"Оплата чека {receipt.id}",
            "pg_salt": uuid.uuid4().hex,
            "pg_result_url": f"{settings.SITE_BASE_URL}/api/integrations/payments/webhook/",
            "pg_request_method": "POST",
            "pg_testing_mode": "0",
        }
        params["pg_sig"] = self._sign(self.INIT_SCRIPT, params)

        resp = requests.post(self.INIT_URL, data=params, timeout=20)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        status = (root.findtext("pg_status") or "").lower()
        if status != "ok":
            msg = root.findtext("pg_error_description") or resp.text
            raise RuntimeError(f"FreedomPay init failed: {msg}")
        return Invoice(
            reference=root.findtext("pg_payment_id") or "",
            payment_url=root.findtext("pg_redirect_url") or "",
        )

    def verify_webhook(self, request) -> dict:
        data = {k: v for k, v in request.data.items()}
        received_sig = data.pop("pg_sig", None)
        expected_sig = self._sign(self.RESULT_SCRIPT, data)
        if received_sig != expected_sig:
            logger.warning("FreedomPay webhook signature mismatch for %s", data.get("pg_order_id"))
            return {"reference": data.get("pg_payment_id", ""), "paid": False}
        paid = str(data.get("pg_result")) == "1"
        return {"reference": data.get("pg_payment_id", ""), "paid": paid}


def get_gateway() -> BasePaymentGateway:
    provider = (settings.PAYMENT_GATEWAY or "mock").lower()
    if provider == "mock":
        return MockGateway()
    if provider == "freedompay":
        return FreedomPayGateway()
    logger.warning("Unknown PAYMENT_GATEWAY '%s' — falling back to mock", provider)
    return MockGateway()
