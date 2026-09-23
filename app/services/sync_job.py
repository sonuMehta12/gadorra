"""Background settler for payments the browser never reported.

A student can pay and then close the tab, lose signal, or never return from the
UPI app. The webhook covers that when it is configured; this job covers it when
it is not, and double-covers it when it is. Everything it calls is idempotent,
so the two can safely overlap.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Payment, PaymentStatus, Registration, RegistrationStatus
from app.services.payments import sync_from_order

log = logging.getLogger("sync_job")


def settle_open_orders(db: Session) -> tuple[int, int]:
    """Returns (checked, settled)."""
    now = datetime.now(timezone.utc)
    not_before = now - timedelta(minutes=settings.sync_min_age_minutes)
    not_after = now - timedelta(hours=settings.registration_expiry_hours)

    open_payments = (
        db.query(Payment)
        .join(Registration, Registration.id == Payment.registration_id)
        .filter(
            Payment.status == PaymentStatus.CREATED,
            Registration.status == RegistrationStatus.PENDING_PAYMENT,
            Payment.created_at <= not_before,
            Payment.created_at >= not_after,
        )
        .order_by(Payment.created_at.asc())
        .limit(settings.sync_batch_size)
        .all()
    )

    settled = 0
    for payment in open_payments:
        try:
            result = sync_from_order(db, payment)
            db.commit()
            if result is not None:
                settled += 1
                log.info(
                    "settled order %s from the sync job -> %s",
                    payment.razorpay_order_id, result.registration.status.value,
                )
        except Exception:
            db.rollback()
            log.exception("sync failed for order %s", payment.razorpay_order_id)

    return len(open_payments), settled


def expire_stale_registrations(db: Session) -> int:
    now = datetime.now(timezone.utc)
    updated = (
        db.query(Registration)
        .filter(
            Registration.status == RegistrationStatus.PENDING_PAYMENT,
            Registration.expires_at.isnot(None),
            Registration.expires_at <= now,
        )
        .update({"status": RegistrationStatus.EXPIRED}, synchronize_session=False)
    )
    db.commit()
    return updated


async def run_forever() -> None:
    interval = settings.sync_interval_minutes * 60
    log.info("payment sync job started, every %s minutes", settings.sync_interval_minutes)
    while True:
        await asyncio.sleep(interval)
        db = SessionLocal()
        try:
            if not settings.razorpay_configured:
                continue
            checked, settled = await asyncio.to_thread(settle_open_orders, db)
            expired = await asyncio.to_thread(expire_stale_registrations, db)
            if checked or expired:
                log.info("sync pass: %s open order(s) checked, %s settled, %s expired", checked, settled, expired)
        except Exception:
            log.exception("sync job pass failed")
        finally:
            db.close()
