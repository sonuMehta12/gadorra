import uuid

import pytest

from app.models import Acknowledgement, Payment, PaymentStatus, RegistrationStatus
from app.services.payments import apply_payment


def _open_payment(db, reg, amount=None):
    order_id = f"order_T{uuid.uuid4().hex[:12]}"
    p = Payment(
        registration_id=uuid.UUID(reg["id"]), razorpay_order_id=order_id,
        amount_paise=amount or reg["fee_amount_paise"], status=PaymentStatus.CREATED,
    )
    db.add(p)
    db.flush()
    return order_id


def _captured(order_id, amount, status="captured"):
    return {
        "id": f"pay_T{uuid.uuid4().hex[:12]}", "order_id": order_id, "status": status,
        "amount": amount, "currency": "INR", "method": "upi",
    }


def test_a_captured_payment_marks_the_registration_paid(db, registration):
    reg, _, _ = registration
    order_id = _open_payment(db, reg)
    data = _captured(order_id, reg["fee_amount_paise"])
    result = apply_payment(db, data["id"], data)
    assert result.registration.status is RegistrationStatus.PAID
    assert result.newly_paid is True
    assert result.acknowledgement is not None


def test_applying_the_same_payment_twice_changes_nothing(db, registration):
    reg, _, _ = registration
    order_id = _open_payment(db, reg)
    data = _captured(order_id, reg["fee_amount_paise"])
    first = apply_payment(db, data["id"], data)
    second = apply_payment(db, data["id"], data)
    assert second.acknowledgement.number == first.acknowledgement.number
    assert second.newly_paid is False
    assert db.query(Acknowledgement).filter(
        Acknowledgement.registration_id == first.registration.id
    ).count() == 1


def test_a_failed_payment_leaves_no_acknowledgement(db, registration):
    reg, _, _ = registration
    order_id = _open_payment(db, reg)
    data = _captured(order_id, reg["fee_amount_paise"], status="failed")
    result = apply_payment(db, data["id"], data)
    assert result.registration.status is RegistrationStatus.FAILED
    assert result.acknowledgement is None


def test_an_amount_that_does_not_match_the_order_is_refused(db, registration):
    reg, _, _ = registration
    order_id = _open_payment(db, reg)
    data = _captured(order_id, 100)  # paid Rs 1 against a Rs 99 order
    with pytest.raises(ValueError):
        apply_payment(db, data["id"], data)
    payment = db.query(Payment).filter(Payment.razorpay_order_id == order_id).one()
    assert payment.status is PaymentStatus.FAILED


def test_an_unknown_order_is_reported(db):
    with pytest.raises(LookupError):
        apply_payment(db, "pay_nope", {"id": "pay_nope", "order_id": "order_nope", "status": "captured", "amount": 1})


def test_creating_an_order_refuses_a_paid_registration(client, paid):
    reg, _, _, _ = paid
    r = client.post("/api/v1/payments/order", json={"registration_id": reg["id"]})
    assert r.status_code == 409


def test_verify_rejects_a_bad_signature(client, registration):
    reg, _, _ = registration
    r = client.post("/api/v1/payments/verify", json={
        "razorpay_order_id": "order_x", "razorpay_payment_id": "pay_x", "razorpay_signature": "nonsense",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "SIGNATURE_INVALID"
