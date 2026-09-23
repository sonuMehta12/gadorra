import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Payment, PaymentStatus, Registration, RegistrationStatus
from app.schemas import OrderIn, OrderOut, VerifyIn, VerifyOut
from app.services import payments as payment_service
from app.services import razorpay_client
from app.services.razorpay_client import RazorpayNotConfigured

log = logging.getLogger("payments.router")
router = APIRouter(prefix="/payments", tags=["payments"])


@router.post(
    "/order",
    response_model=OrderOut,
    summary="Create a Razorpay order",
    description="Returns `razorpay_order_id`, the amount in paise and the public key. "
                "Pass them to Razorpay Checkout. Calling again for an unpaid registration "
                "reuses the open order instead of creating another.",
    responses={
        404: {"description": "NOT_FOUND"},
        409: {"description": "ALREADY_PAID"},
        502: {"description": "RAZORPAY_ERROR"},
        503: {"description": "RAZORPAY_NOT_CONFIGURED"},
    },
)
def create_order(payload: OrderIn, db: Session = Depends(get_db)) -> OrderOut:
    registration = (
        db.query(Registration)
        .filter(Registration.id == payload.registration_id)
        .with_for_update()
        .one_or_none()
    )
    if registration is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Registration not found"})
    if registration.status is RegistrationStatus.PAID:
        raise HTTPException(
            status_code=409, detail={"code": "ALREADY_PAID", "message": "This registration is already paid"}
        )

    # Reuse an open order instead of creating a new one on every retry.
    existing = (
        db.query(Payment)
        .filter(Payment.registration_id == registration.id, Payment.status == PaymentStatus.CREATED)
        .order_by(Payment.created_at.desc())
        .first()
    )
    if existing and existing.amount_paise == registration.fee_amount_paise:
        return OrderOut(
            razorpay_order_id=existing.razorpay_order_id,
            amount_paise=existing.amount_paise,
            currency="INR",
            razorpay_key_id=settings.razorpay_key_id,
            registration_id=registration.id,
        )

    try:
        order = razorpay_client.create_order(
            amount_paise=registration.fee_amount_paise,
            receipt=str(registration.id),
            notes={"registration_id": str(registration.id), "phase": registration.phase.value},
        )
    except RazorpayNotConfigured as exc:
        raise HTTPException(status_code=503, detail={"code": "RAZORPAY_NOT_CONFIGURED", "message": str(exc)})
    except Exception as exc:
        log.exception("razorpay order create failed")
        raise HTTPException(status_code=502, detail={"code": "RAZORPAY_ERROR", "message": str(exc)})

    payment = Payment(
        registration_id=registration.id,
        razorpay_order_id=order["id"],
        amount_paise=registration.fee_amount_paise,
        status=PaymentStatus.CREATED,
    )
    db.add(payment)
    db.commit()

    return OrderOut(
        razorpay_order_id=order["id"],
        amount_paise=registration.fee_amount_paise,
        currency="INR",
        razorpay_key_id=settings.razorpay_key_id,
        registration_id=registration.id,
    )


@router.post(
    "/verify",
    response_model=VerifyOut,
    summary="Settle a payment after Checkout",
    description="Send Checkout's `razorpay_order_id`, `razorpay_payment_id` and "
                "`razorpay_signature`. The signature is verified, then the payment is fetched "
                "from Razorpay server to server. On success the response carries the "
                "acknowledgement number. Safe to call twice -- nothing is duplicated.",
    responses={
        400: {"description": "SIGNATURE_INVALID"},
        404: {"description": "ORDER_UNKNOWN"},
        422: {"description": "AMOUNT_MISMATCH -- the captured amount is not the order amount"},
        502: {"description": "RAZORPAY_ERROR"},
    },
)
def verify(payload: VerifyIn, db: Session = Depends(get_db)) -> VerifyOut:
    """Fast path after checkout. Signature is checked, then the status is fetched
    from Razorpay server to server -- the browser's word is never enough."""
    if not razorpay_client.verify_checkout_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "SIGNATURE_INVALID", "message": "Payment signature could not be verified"},
        )

    try:
        result = payment_service.apply_payment(db, payload.razorpay_payment_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail={"code": "ORDER_UNKNOWN", "message": str(exc)})
    except ValueError as exc:
        db.commit()
        raise HTTPException(status_code=422, detail={"code": "AMOUNT_MISMATCH", "message": str(exc)})
    except Exception as exc:
        db.rollback()
        log.exception("verify failed")
        raise HTTPException(status_code=502, detail={"code": "RAZORPAY_ERROR", "message": str(exc)})

    db.commit()

    if result.registration.status is not RegistrationStatus.PAID:
        return VerifyOut(
            status=result.registration.status.value,
            acknowledgement_number=None,
            registration_id=result.registration.id,
            message="Payment is not captured yet. If money was deducted it will settle shortly.",
        )

    return VerifyOut(
        status="PAID",
        acknowledgement_number=result.acknowledgement.number if result.acknowledgement else None,
        registration_id=result.registration.id,
        message="Payment confirmed" if result.newly_paid else "Payment already confirmed",
    )


@router.post("/{payment_id}/sync", response_model=VerifyOut,
             summary="Re-fetch a payment from Razorpay (admin)")
def sync(payment_id: UUID, db: Session = Depends(get_db)) -> VerifyOut:
    """Admin: re-ask Razorpay what happened to this order."""
    payment = db.query(Payment).filter(Payment.id == payment_id).one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Payment not found"})

    try:
        result = payment_service.sync_from_order(db, payment)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail={"code": "RAZORPAY_ERROR", "message": str(exc)})

    db.commit()
    if result is None:
        return VerifyOut(
            status=payment.status.value,
            acknowledgement_number=None,
            registration_id=payment.registration_id,
            message="No captured payment found for this order",
        )
    return VerifyOut(
        status=result.registration.status.value,
        acknowledgement_number=result.acknowledgement.number if result.acknowledgement else None,
        registration_id=result.registration.id,
        message="Synced from Razorpay",
    )
