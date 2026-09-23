"""Exercise the payment finalisation path without touching Razorpay.

Fabricates a 'captured' payment for a registration and runs the same code the
real webhook and /payments/verify run. Use only in development.

    python scripts/simulate_payment.py <registration_id>
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.models import Payment, PaymentStatus, Registration  # noqa: E402
from app.services.payments import apply_payment  # noqa: E402


def main(registration_id: str) -> None:
    db = SessionLocal()
    try:
        reg = db.query(Registration).filter(Registration.id == uuid.UUID(registration_id)).one()
        order_id = f"order_SIM{uuid.uuid4().hex[:11]}"
        payment_id = f"pay_SIM{uuid.uuid4().hex[:11]}"

        db.add(
            Payment(
                registration_id=reg.id,
                razorpay_order_id=order_id,
                amount_paise=reg.fee_amount_paise,
                status=PaymentStatus.CREATED,
            )
        )
        db.commit()

        fake = {
            "id": payment_id,
            "order_id": order_id,
            "status": "captured",
            "amount": reg.fee_amount_paise,
            "currency": "INR",
            "method": "upi",
            "simulated": True,
        }

        r1 = apply_payment(db, payment_id, fake)
        db.commit()
        print(f"1st call  -> status={r1.registration.status.value} "
              f"ack={r1.acknowledgement.number} newly_paid={r1.newly_paid}")

        r2 = apply_payment(db, payment_id, fake)
        db.commit()
        print(f"2nd call  -> status={r2.registration.status.value} "
              f"ack={r2.acknowledgement.number} newly_paid={r2.newly_paid}")

        assert r1.acknowledgement.number == r2.acknowledgement.number, "ack number changed on replay!"
        print("idempotent: same acknowledgement number, no second notification")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python scripts/simulate_payment.py <registration_id>")
    main(sys.argv[1])
