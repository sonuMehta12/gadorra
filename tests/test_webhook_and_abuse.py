import hashlib
import hmac
import json
import uuid

from app.config import settings
from app.models import Payment, PaymentStatus, RegistrationStatus, WebhookEvent


def _sign(body: bytes) -> str:
    return hmac.new(settings.razorpay_webhook_secret.encode(), body, hashlib.sha256).hexdigest()


def _event(order_id: str, payment_id: str, amount: int) -> dict:
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": payment_id, "order_id": order_id, "status": "captured",
            "amount": amount, "currency": "INR", "method": "upi",
        }}},
    }


def test_an_unsigned_webhook_is_stored_but_not_acted_on(client, db, registration):
    reg, _, _ = registration
    body = json.dumps(_event("order_x", "pay_x", 9900)).encode()
    r = client.post("/api/v1/webhooks/razorpay", content=body,
                    headers={"Content-Type": "application/json", "X-Razorpay-Event-Id": "evt_bad"})
    assert r.json()["status"] == "signature_invalid"
    stored = db.query(WebhookEvent).filter(WebhookEvent.event_id == "evt_bad").one()
    assert stored.signature_valid is False
    assert stored.processed is False


def test_a_signed_webhook_settles_the_registration(client, db, registration):
    reg, _, _ = registration
    order_id = f"order_T{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_T{uuid.uuid4().hex[:12]}"
    db.add(Payment(registration_id=uuid.UUID(reg["id"]), razorpay_order_id=order_id,
                   amount_paise=reg["fee_amount_paise"], status=PaymentStatus.CREATED))
    db.flush()

    body = json.dumps(_event(order_id, payment_id, reg["fee_amount_paise"])).encode()
    r = client.post("/api/v1/webhooks/razorpay", content=body, headers={
        "Content-Type": "application/json",
        "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:8]}",
        "X-Razorpay-Signature": _sign(body),
    })
    assert r.json()["status"] == "processed"

    from app.models import Registration
    row = db.query(Registration).filter(Registration.id == uuid.UUID(reg["id"])).one()
    assert row.status is RegistrationStatus.PAID
    assert row.acknowledgement is not None


def test_the_same_event_delivered_twice_is_ignored(client, db, registration):
    reg, _, _ = registration
    order_id = f"order_T{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_T{uuid.uuid4().hex[:12]}"
    db.add(Payment(registration_id=uuid.UUID(reg["id"]), razorpay_order_id=order_id,
                   amount_paise=reg["fee_amount_paise"], status=PaymentStatus.CREATED))
    db.flush()

    body = json.dumps(_event(order_id, payment_id, reg["fee_amount_paise"])).encode()
    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    headers = {"Content-Type": "application/json", "X-Razorpay-Event-Id": event_id,
               "X-Razorpay-Signature": _sign(body)}

    first = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    second = client.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "duplicate_ignored"

    from app.models import Acknowledgement
    assert db.query(Acknowledgement).filter(
        Acknowledgement.registration_id == uuid.UUID(reg["id"])
    ).count() == 1


def test_an_unrelated_event_type_is_acknowledged_and_skipped(client):
    body = json.dumps({"event": "subscription.charged", "payload": {}}).encode()
    r = client.post("/api/v1/webhooks/razorpay", content=body, headers={
        "Content-Type": "application/json",
        "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:8]}",
        "X-Razorpay-Signature": _sign(body),
    })
    assert r.json()["status"] == "ignored"


def test_bot_check_passes_when_no_provider_is_configured():
    from app.services import bot_check
    ok, reason = bot_check.verify(None)
    assert ok is True and reason == "disabled"


def test_bot_check_rejects_a_missing_token_when_a_provider_is_set(monkeypatch):
    from app.services import bot_check
    monkeypatch.setattr(settings, "bot_check_provider", "turnstile")
    monkeypatch.setattr(settings, "bot_check_secret", "s3cret")
    ok, reason = bot_check.verify(None)
    assert ok is False
    assert "missing" in reason


def test_bot_check_lets_traffic_through_if_the_provider_is_unreachable(monkeypatch):
    from app.services import bot_check
    monkeypatch.setattr(settings, "bot_check_provider", "turnstile")
    monkeypatch.setattr(settings, "bot_check_secret", "s3cret")

    class Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): raise RuntimeError("down")

    monkeypatch.setattr(bot_check.httpx, "Client", lambda **k: Boom())
    ok, reason = bot_check.verify("token")
    assert ok is True and "unreachable" in reason


def test_the_rate_limiter_counts_and_resets():
    from app.rate_limit import RateLimiter
    rl = RateLimiter()
    for _ in range(3):
        assert rl.hit("k", 3, 60)[0] is True
    allowed, retry_after = rl.hit("k", 3, 60)
    assert allowed is False and retry_after > 0
    assert rl.hit("other-key", 3, 60)[0] is True
