import logging

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import WebhookEvent
from app.services import payments as payment_service
from app.services import razorpay_client

log = logging.getLogger("webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

HANDLED = {"payment.captured", "order.paid", "payment.failed", "refund.processed"}


@router.post("/razorpay", summary="Razorpay payment events",
             description="Configured in the Razorpay dashboard. Not called by the front end.")
async def razorpay_webhook(
    request: Request,
    x_razorpay_event_id: str | None = Header(default=None, alias="X-Razorpay-Event-Id"),
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    db: Session = Depends(get_db),
) -> dict:
    """Safety net: settles the registration even if the browser never came back.

    Always returns 200 once the event is stored, so Razorpay does not retry
    forever on a bug of ours. The stored row is the audit trail.
    """
    body = await request.body()
    payload = await request.json()

    event_id = x_razorpay_event_id or payload.get("id") or ""
    event_type = payload.get("event")
    valid = razorpay_client.verify_webhook_signature(body, x_razorpay_signature or "")

    existing = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).one_or_none()
    if existing is not None:
        return {"status": "duplicate_ignored", "event_id": event_id}

    event = WebhookEvent(
        provider="razorpay",
        event_id=event_id or f"unsigned-{id(body)}",
        event_type=event_type,
        signature_valid=valid,
        payload=payload,
    )
    db.add(event)
    db.commit()

    if not valid:
        log.error("razorpay webhook with an invalid signature, event=%s", event_type)
        return {"status": "signature_invalid"}

    if event_type not in HANDLED:
        event.processed = True
        db.commit()
        return {"status": "ignored", "event": event_type}

    entity = (
        payload.get("payload", {}).get("payment", {}).get("entity")
        or payload.get("payload", {}).get("refund", {}).get("entity")
        or {}
    )
    payment_id = entity.get("payment_id") or entity.get("id")
    if not payment_id:
        event.error = "no payment id in payload"
        db.commit()
        return {"status": "no_payment_id"}

    try:
        payment_data = entity if entity.get("status") else None
        payment_service.apply_payment(db, payment_id, payment_data)
        event.processed = True
        db.commit()
    except Exception as exc:
        db.rollback()
        log.exception("webhook processing failed")
        event = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).one_or_none()
        if event:
            event.error = repr(exc)
            db.commit()
        return {"status": "error", "detail": str(exc)}

    return {"status": "processed", "event": event_type}
