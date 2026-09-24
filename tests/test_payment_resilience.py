"""Regressions from the first real payments on the deployed API.

A 26-character email overflowed notifications.recipient (VARCHAR(20)); the
exception rolled back the whole settlement, so Razorpay held the money while the
registration stayed PENDING, and the raw SQL -- student email included -- went
back to the browser.
"""
import uuid

from app.models import Notification, Payment, PaymentStatus, Registration, RegistrationStatus
from app.services.payments import apply_payment


def _settle(db, reg):
    order_id = f"order_T{uuid.uuid4().hex[:12]}"
    db.add(Payment(registration_id=uuid.UUID(reg["id"]), razorpay_order_id=order_id,
                   amount_paise=reg["fee_amount_paise"], status=PaymentStatus.CREATED))
    db.flush()
    pid = f"pay_T{uuid.uuid4().hex[:12]}"
    return apply_payment(db, pid, {"id": pid, "order_id": order_id, "status": "captured",
                                   "amount": reg["fee_amount_paise"], "currency": "INR"})


def test_a_long_email_address_is_stored_and_recorded_as_email(db, registration):
    reg, _, _ = registration
    result = _settle(db, reg)
    email_rows = db.query(Notification).filter(
        Notification.registration_id == result.registration.id,
        Notification.channel == "email",
    ).all()
    assert len(email_rows) == 1
    assert email_rows[0].recipient == "mohammad.student.long.address@example.com"


def test_a_notification_failure_does_not_undo_the_payment(db, registration, monkeypatch):
    from app.services import payments as payments_service

    def explode(*a, **k):
        raise RuntimeError("smtp and whatsapp both on fire")

    monkeypatch.setattr(payments_service, "send_acknowledgement", explode)
    reg, _, _ = registration
    result = _settle(db, reg)

    assert result.registration.status is RegistrationStatus.PAID
    assert result.acknowledgement is not None
    row = db.query(Registration).filter(Registration.id == uuid.UUID(reg["id"])).one()
    assert row.status is RegistrationStatus.PAID


def test_verify_never_returns_database_internals(client, registration, monkeypatch):
    import hmac, hashlib
    from app.config import settings
    from app.services import payments as payments_service

    def broken(*a, **k):
        raise RuntimeError("(psycopg.errors.Something) INSERT INTO notifications ... secret@example.com")

    monkeypatch.setattr(payments_service, "apply_payment", broken)
    order_id, payment_id = "order_leaktest", "pay_leaktest"
    sig = hmac.new(settings.razorpay_key_secret.encode(), f"{order_id}|{payment_id}".encode(),
                   hashlib.sha256).hexdigest()
    r = client.post("/api/v1/payments/verify", json={
        "razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": sig,
    })
    assert r.status_code == 502
    text = r.text.lower()
    for leaked in ("psycopg", "insert into", "notifications", "secret@example.com"):
        assert leaked not in text, f"{leaked!r} leaked to the client"
    assert r.json()["detail"]["code"] == "VERIFY_FAILED"


def test_whatsapp_goes_first_and_empty_smtp_makes_no_network_call(db, paid, monkeypatch):
    from app.config import settings
    from app.services import notifications as notif

    _, _, _, result = paid
    db.query(Notification).delete()
    db.flush()

    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "smtp_password", "")

    def no_network():
        raise AssertionError("an SMTP connection was attempted with no password")

    monkeypatch.setattr(notif, "get_email_provider", no_network)
    sent = notif.send_acknowledgement(db, result.registration, "GPET26/UP49/11111")

    assert [n.channel for n in sent] == ["whatsapp", "email"]
    assert sent[0].status.value == "SENT"
    assert sent[1].status.value == "FAILED"
    assert sent[1].error.startswith("EMAIL_NOT_CONFIGURED")
