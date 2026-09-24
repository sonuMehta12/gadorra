"""WhatsApp delivery with the limits Meta actually enforces.

Three guards sit in front of every send:
  consent   -- consent_whatsapp false means we never message that student
  throughput-- Meta rejects more than one message per 6 seconds to the same user
  messaging -- a phone number may reach only N unique users per rolling 24 hours
               (250 on a fresh number, 2,000 after business verification)

A blocked send is recorded as a notification row with a reason, never silently
dropped, so an admin can resend once the window or the cap frees up.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Notification, NotificationStatus, Registration
from app.services.email import Attachment, get_provider as get_email_provider
from app.services.whatsapp import get_provider, to_e164

log = logging.getLogger("notifications")


def _record(db: Session, registration_id, template: str, recipient: str, payload: dict,
            channel: str = "whatsapp") -> Notification:
    n = Notification(
        registration_id=registration_id,
        channel=channel,
        template=template,
        recipient=recipient,
        payload=payload,
        status=NotificationStatus.QUEUED,
    )
    db.add(n)
    db.flush()
    return n


def _block(db: Session, n: Notification, code: str, detail: str) -> Notification:
    n.status = NotificationStatus.FAILED
    n.error = f"{code}: {detail}"
    db.flush()
    log.warning("whatsapp send blocked -- %s: %s", code, detail)
    return n


def _too_soon(db: Session, recipient: str) -> float | None:
    """Seconds still to wait before this number may be messaged again."""
    gap = settings.whatsapp_min_seconds_between_messages
    if gap <= 0:
        return None
    last = (
        db.query(Notification.created_at)
        .filter(
            Notification.recipient == recipient,
            Notification.status == NotificationStatus.SENT,
        )
        .order_by(Notification.created_at.desc())
        .first()
    )
    if last is None:
        return None
    elapsed = (datetime.now(timezone.utc) - last[0]).total_seconds()
    return round(gap - elapsed, 1) if elapsed < gap else None


def _cap_reached(db: Session, recipient: str) -> bool:
    """True when messaging this NEW recipient would cross the 24h unique-user cap."""
    cap = settings.whatsapp_daily_unique_recipients
    if cap <= 0:
        return False
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    reached_already = (
        db.query(Notification.id)
        .filter(
            Notification.recipient == recipient,
            Notification.status == NotificationStatus.SENT,
            Notification.created_at >= since,
        )
        .first()
    )
    if reached_already:
        return False  # already counted inside this window, costs nothing more
    distinct = (
        db.query(func.count(func.distinct(Notification.recipient)))
        .filter(Notification.status == NotificationStatus.SENT, Notification.created_at >= since)
        .scalar()
    ) or 0
    return distinct >= cap


def _dispatch(db: Session, n: Notification, send, check_limits: bool = True) -> Notification:
    if not check_limits:
        return _deliver(db, n, send)

    waiting = _too_soon(db, n.recipient)
    if waiting is not None:
        return _block(db, n, "THROUGHPUT", f"only 1 message per {settings.whatsapp_min_seconds_between_messages}s per user, {waiting}s left")
    if _cap_reached(db, n.recipient):
        return _block(db, n, "MESSAGING_LIMIT", f"{settings.whatsapp_daily_unique_recipients} unique recipients already messaged in the last 24h")

    return _deliver(db, n, send)


def _deliver(db: Session, n: Notification, send) -> Notification:
    n.attempts += 1
    result = send()
    if result.ok:
        n.status = NotificationStatus.SENT
        n.provider_message_id = result.provider_message_id
        n.error = None
    else:
        n.status = NotificationStatus.FAILED
        n.error = result.error
        log.error("%s send failed: %s", n.channel, result.error)
    db.flush()
    return n


def send_otp(db: Session, mobile: str, code: str) -> Notification:
    provider = get_provider()
    to = to_e164(mobile)
    channel = settings.otp_channel

    if channel == "whatsapp_template":
        template = settings.whatsapp_template_otp
        n = _record(db, None, template, to, {"code": "***"})
        return _dispatch(db, n, lambda: provider.send_authentication(to, template, code))

    if channel == "whatsapp_text":
        body = (
            f"{code} is your GPET verification code. "
            f"It expires in {settings.otp_ttl_minutes} minutes. Do not share it."
        )
        n = _record(db, None, "otp_text", to, {"code": "***"})
        return _dispatch(db, n, lambda: provider.send_text(to, body))

    # console: never touches the network, prints to the server log
    n = _record(db, None, "otp_console", to, {"code": "***"})
    log.warning("[OTP] mobile=%s code=%s", mobile, code)
    n.status = NotificationStatus.SENT
    n.provider_message_id = "console"
    n.attempts += 1
    db.flush()
    return n


def send_acknowledgement(db: Session, registration: Registration, number: str) -> list[Notification]:
    """Deliver the acknowledgement number on every channel we have.

    Email is the dependable one today -- the WhatsApp template is still in review,
    and a free-form message only lands inside a 24-hour customer service window,
    which a new student will not have. Both are attempted; one failing never stops
    the other, and every attempt is recorded.
    """
    sent: list[Notification] = []
    # WhatsApp first: it is the channel that works today, and email can stall on
    # a slow SMTP login while the student waits on the payment screen.
    ack_whatsapp = send_acknowledgement_whatsapp(db, registration, number)
    if ack_whatsapp is not None:
        sent.append(ack_whatsapp)
    ack_email = send_acknowledgement_email(db, registration, number)
    if ack_email is not None:
        sent.append(ack_email)
    return sent


def send_acknowledgement_whatsapp(db: Session, registration: Registration, number: str) -> Notification | None:
    provider = get_provider()
    student = registration.student
    to = to_e164(student.mobile)
    amount = f"{registration.fee_amount_paise / 100:.0f}"

    if not student.consent_whatsapp:
        n = _record(db, registration.id, "acknowledgement", to, {"number": number})
        return _block(db, n, "NO_CONSENT", "student did not consent to WhatsApp messages")

    body = (
        f"Hi {student.full_name}, your GPET registration is confirmed.\n"
        f"Class: {student.class_level}\n"
        f"Amount paid: Rs {amount}\n"
        f"Acknowledgement number: {number}\n"
        f"Keep this number safe -- you will need it at final registration."
    )

    use_template = (
        settings.whatsapp_provider == "meta"
        and settings.whatsapp_ack_channel == "template"
        and settings.whatsapp_template_ack
    )

    if use_template:
        template = settings.whatsapp_template_ack
        params = [student.full_name, amount, number]
        n = _record(db, registration.id, template, to, {"params": params})
        _dispatch(db, n, lambda: provider.send_template(to, template, params))
        if n.status is NotificationStatus.SENT:
            return n
        if n.error and n.error.startswith(("THROUGHPUT", "MESSAGING_LIMIT")):
            return n  # a limit, not a template problem -- retrying as text would fail too
        log.warning("template %s failed, falling back to a free-form message", template)

    n = _record(db, registration.id, "acknowledgement_text", to, {"number": number})
    return _dispatch(db, n, lambda: provider.send_text(to, body))


def send_acknowledgement_email(db: Session, registration: Registration, number: str) -> Notification | None:
    if not settings.email_enabled:
        return None

    student = registration.student
    if not student.email:
        return None

    amount = f"{registration.fee_amount_paise / 100:.0f}"
    subject = f"GPET 2026 registration confirmed -- {number}"
    text = (
        f"Hi {student.full_name},\n\n"
        f"Your GPET 2026 pre-registration is confirmed.\n\n"
        f"Acknowledgement number: {number}\n"
        f"Class: {student.class_level}\n"
        f"Amount paid: Rs {amount}\n\n"
        f"Keep this number safe. You will need it at final registration, where it also\n"
        f"gets you the returning-student fee.\n\n"
        f"Your receipt is attached.\n\n"
        f"Gradorra Private Limited\n"
    )
    html = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;color:#14161a;line-height:1.6">
  <p>Hi {student.full_name},</p>
  <p>Your GPET 2026 pre-registration is confirmed.</p>
  <div style="border:1px dashed #e8681f;background:#fdf3ec;border-radius:8px;padding:14px;text-align:center;margin:18px 0">
    <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#5f6672">Acknowledgement number</div>
    <div style="font-family:ui-monospace,Menlo,monospace;font-size:20px;font-weight:700;margin-top:4px">{number}</div>
  </div>
  <p>Class {student.class_level} &middot; Amount paid Rs {amount}</p>
  <p>Keep this number safe. You will need it at final registration, where it also gets
     you the returning-student fee.</p>
  <p style="color:#5f6672;font-size:13px">Your receipt is attached.<br>Gradorra Private Limited</p>
</div>"""

    attachments = []
    if settings.email_attach_receipt:
        try:
            from app.models import Payment, PaymentStatus
            from app.services import receipt as receipt_service

            payment = (
                db.query(Payment)
                .filter(Payment.registration_id == registration.id, Payment.status == PaymentStatus.CAPTURED)
                .order_by(Payment.updated_at.desc())
                .first()
            )
            data = receipt_service.build(registration, payment)
            attachments.append(Attachment(f"{data['receipt_id']}.pdf", receipt_service.to_pdf(data)))
        except Exception:
            # A receipt that will not render must not stop the number reaching the student.
            log.exception("could not attach the receipt, sending the email without it")

    n = _record(db, registration.id, "acknowledgement_email", student.email, {"number": number},
                channel="email")
    if settings.email_provider == "smtp" and not settings.smtp_password:
        # Skip the network round trip entirely; record why, so it can be resent later.
        return _block(db, n, "EMAIL_NOT_CONFIGURED", "SMTP_PASSWORD is empty")

    provider = get_email_provider()
    return _dispatch(
        db, n,
        lambda: provider.send(student.email, subject, text, html, attachments),
        check_limits=False,  # Meta's limits are WhatsApp's, not email's
    )
