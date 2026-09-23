"""Razorpay wrapper. Every amount comes from the server, never from the browser."""
import hashlib
import hmac
import logging

import razorpay

from app.config import settings

log = logging.getLogger("razorpay")


class RazorpayNotConfigured(RuntimeError):
    pass


def _client() -> razorpay.Client:
    if not settings.razorpay_configured:
        raise RazorpayNotConfigured(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are empty -- add the test keys to .env"
        )
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


def create_order(amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
    return _client().order.create(
        {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt[:40],
            "payment_capture": 1,
            "notes": notes or {},
        }
    )


def fetch_payment(payment_id: str) -> dict:
    """Server-to-server truth. Never trust the browser's word that a payment succeeded."""
    return _client().payment.fetch(payment_id)


def fetch_order_payments(order_id: str) -> list[dict]:
    return _client().order.payments(order_id).get("items", [])


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not settings.razorpay_key_secret:
        return False
    expected = hmac.new(
        settings.razorpay_key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    secret = settings.razorpay_webhook_secret
    if not secret:
        log.warning("RAZORPAY_WEBHOOK_SECRET is empty -- webhook signature cannot be verified")
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
