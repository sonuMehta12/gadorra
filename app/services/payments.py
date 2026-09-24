"""Payment finalisation -- the one place a registration becomes PAID.

Called from three entry points, all idempotent on the Razorpay payment id:
  POST /payments/verify   browser came back (fast path)
  POST /webhooks/razorpay Razorpay tells us (safety net when the browser never returns)
  POST /payments/{id}/sync admin re-fetch
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    Acknowledgement,
    Payment,
    PaymentStatus,
    Registration,
    RegistrationStatus,
)
from app.services import razorpay_client
from app.services.acknowledgement import create_for_registration
from app.services.notifications import send_acknowledgement

log = logging.getLogger("payments")

_RZP_STATUS = {
    "created": PaymentStatus.CREATED,
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "failed": PaymentStatus.FAILED,
    "refunded": PaymentStatus.REFUNDED,
}


class PaymentResult:
    def __init__(self, registration: Registration, acknowledgement: Acknowledgement | None, newly_paid: bool):
        self.registration = registration
        self.acknowledgement = acknowledgement
        self.newly_paid = newly_paid


def apply_payment(db: Session, payment_id: str, rzp_payment: dict | None = None) -> PaymentResult:
    """Take a Razorpay payment id, fetch the truth, and settle the registration."""
    if rzp_payment is None:
        rzp_payment = razorpay_client.fetch_payment(payment_id)

    order_id = rzp_payment.get("order_id")
    payment = (
        db.query(Payment)
        .filter(Payment.razorpay_order_id == order_id)
        .with_for_update()
        .one_or_none()
    )
    if payment is None:
        raise LookupError(f"no payment row for razorpay order {order_id}")

    registration = (
        db.query(Registration)
        .filter(Registration.id == payment.registration_id)
        .with_for_update()
        .one()
    )

    payment.razorpay_payment_id = payment_id
    payment.status = _RZP_STATUS.get(rzp_payment.get("status", ""), PaymentStatus.CREATED)
    payment.method = rzp_payment.get("method")
    payment.raw_response = rzp_payment

    # Amount tamper check: what Razorpay captured must equal what we asked for.
    captured_amount = rzp_payment.get("amount")
    if captured_amount is not None and int(captured_amount) != payment.amount_paise:
        log.error(
            "amount mismatch for order %s: expected %s, captured %s",
            order_id, payment.amount_paise, captured_amount,
        )
        payment.status = PaymentStatus.FAILED
        db.flush()
        raise ValueError("payment amount does not match the order amount")

    if payment.status is not PaymentStatus.CAPTURED:
        if payment.status is PaymentStatus.FAILED:
            registration.status = RegistrationStatus.FAILED
        db.flush()
        return PaymentResult(registration, registration.acknowledgement, newly_paid=False)

    # Already settled -> return what exists, send nothing again.
    if registration.status is RegistrationStatus.PAID and registration.acknowledgement is not None:
        db.flush()
        return PaymentResult(registration, registration.acknowledgement, newly_paid=False)

    registration.status = RegistrationStatus.PAID
    registration.paid_at = datetime.now(timezone.utc)

    ack = create_for_registration(db, registration)

    # Mark a redeemed pre-launch acknowledgement as used, now that money has landed.
    if registration.redeemed_acknowledgement:
        source = (
            db.query(Acknowledgement)
            .filter(Acknowledgement.number == registration.redeemed_acknowledgement)
            .with_for_update()
            .one_or_none()
        )
        if source and not source.redeemed:
            source.redeemed = True
            source.redeemed_by_registration_id = registration.id
            source.redeemed_at = datetime.now(timezone.utc)

    db.flush()

    # The money has landed and the number exists; nothing about messaging may undo
    # that. Notifications run in a savepoint, so a failure rolls back only their
    # own rows and the registration stays PAID. The number is on the screen and
    # the receipt either way, and an admin can resend.
    try:
        with db.begin_nested():
            send_acknowledgement(db, registration, ack.number)
    except Exception:
        log.exception("acknowledgement delivery failed for %s; payment stays settled", ack.number)

    return PaymentResult(registration, ack, newly_paid=True)


def sync_from_order(db: Session, payment: Payment) -> PaymentResult | None:
    """Ask Razorpay what happened to this order. Used when the browser never came back."""
    items = razorpay_client.fetch_order_payments(payment.razorpay_order_id)
    captured = next((p for p in items if p.get("status") == "captured"), None)
    if not captured:
        return None
    return apply_payment(db, captured["id"], captured)
