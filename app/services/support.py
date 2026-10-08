"""Support requests from the student portal, emailed to the team.

The ticket is saved first and the email sent second, so a request survives an
email outage: the sync job retries unsent tickets on its next passes. The email
is built from the database, never from what the browser claims, so the team
sees the real name, number and acknowledgement for the verified mobile.
"""
import html
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.logging_setup import IST, mask_mobile
from app.models import NotificationStatus, Registration, RegistrationStatus, Student, SupportTicket
from app.services.email import Attachment, get_provider, not_configured

log = logging.getLogger("support")

# value -> what the team and the student read. The frontend dropdown comes from
# GET /support/categories, so adding one here is the only change needed.
CATEGORIES = {
    "PROFILE_CORRECTION": "Profile correction (name, father's name, class, district)",
    "PAYMENT": "Payment issue",
    "ACKNOWLEDGEMENT": "Acknowledgement number or receipt",
    "LOGIN_OTP": "Login or OTP problem",
    "TECHNICAL": "Website not working",
    "FEEDBACK": "Feedback or suggestion",
    "OTHER": "Something else",
}

# Identified by their first bytes, not by the name or type the browser sends.
_SIGNATURES = [
    (b"%PDF-", "application/pdf", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
]

MAX_EMAIL_ATTEMPTS = 5
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread on a call


def detect_type(content: bytes) -> tuple[str, str] | None:
    """(mime type, extension) for a PDF, PNG, JPEG or WebP; None for anything else."""
    for magic, mime, ext in _SIGNATURES:
        if content.startswith(magic):
            return mime, ext
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def safe_filename(name: str | None, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]", "", (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    stem = stem.rsplit(".", 1)[0].strip()[:80] or "attachment"
    return f"{stem}.{ext}"


def sent_today(db: Session, mobile: str) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    return (
        db.query(SupportTicket)
        .filter(SupportTicket.mobile == mobile, SupportTicket.created_at >= since)
        .count()
    )


def create_ticket(db: Session, mobile: str, category: str, description: str,
                  attachment: tuple[str, str, bytes] | None) -> SupportTicket:
    """Save the ticket. attachment is (filename, mime type, bytes)."""
    student = db.query(Student).filter(Student.mobile == mobile).one_or_none()
    for _ in range(10):
        ticket = SupportTicket(
            number="SUP-" + "".join(secrets.choice(_ALPHABET) for _ in range(6)),
            mobile=mobile,
            student_id=student.id if student else None,
            category=category,
            description=description,
        )
        if attachment:
            ticket.attachment_name, ticket.attachment_type, ticket.attachment = attachment
        try:
            with db.begin_nested():
                db.add(ticket)
                db.flush()
            break
        except IntegrityError:  # the same random number twice; draw again
            continue
    else:
        raise RuntimeError("could not generate a unique support ticket number")
    db.commit()
    log.info("support ticket created", extra={
        "ticket": ticket.number, "category": category, "mobile": mask_mobile(mobile),
        "attachment": bool(attachment),
    })
    return ticket


def _student_lines(db: Session, ticket: SupportTicket) -> list[tuple[str, str]]:
    student = ticket.student
    if student is None:
        return [("Mobile", ticket.mobile), ("Registered", "No -- this mobile has not submitted the form")]

    registration = (
        db.query(Registration)
        .filter(Registration.student_id == student.id)
        .order_by(Registration.created_at.desc())
        .first()
    )
    paid = (
        db.query(Registration)
        .filter(Registration.student_id == student.id, Registration.status == RegistrationStatus.PAID)
        .order_by(Registration.created_at.asc())
        .first()
    )
    return [
        ("Name", student.full_name),
        ("Father's name", student.father_name or "-"),
        ("Mobile", student.mobile),
        ("Email", student.email or "-"),
        ("Class", str(student.class_level)),
        ("District", student.district.name if student.district else "-"),
        ("Registration", registration.status.value if registration else "-"),
        ("Acknowledgement", paid.acknowledgement.number if paid and paid.acknowledgement else "Not paid yet"),
    ]


def compose(db: Session, ticket: SupportTicket) -> tuple[str, str, str, str | None]:
    """(subject, text, html, reply-to) for the team's email."""
    category = CATEGORIES.get(ticket.category, ticket.category)
    who = ticket.student.full_name if ticket.student else ticket.mobile
    subject = f"[GPET Support] {category.split(' (')[0]} - {who} - {ticket.number}"
    submitted = ticket.created_at.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    rows = [("Ticket", ticket.number), ("Category", category), ("Submitted", submitted)]
    rows += _student_lines(db, ticket)
    if ticket.attachment_name:
        size_kb = round(len(ticket.attachment or b"") / 1024)
        rows.append(("Attachment", f"{ticket.attachment_name} ({size_kb} KB, attached)"))

    width = max(len(k) for k, _ in rows)
    text = (
        "A student sent a request from the GPET portal.\n\n"
        + "\n".join(f"{k.ljust(width)}  {v}" for k, v in rows)
        + "\n\nMessage:\n" + ticket.description
        + "\n\nReply to this email to answer the student"
        + (" directly." if ticket.student and ticket.student.email else "; they gave no email, so call them.")
        + "\n"
    )

    # Everything the student typed is escaped: their message is shown, never run.
    e = html.escape
    table = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#5f6672;white-space:nowrap;vertical-align:top">{e(k)}</td>'
        f'<td style="padding:6px 0">{e(v)}</td></tr>'
        for k, v in rows
    )
    body = e(ticket.description).replace("\n", "<br>")
    html_body = f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;color:#14161a;line-height:1.55">
  <p style="margin:0 0 14px">A student sent a request from the GPET portal.</p>
  <table style="border-collapse:collapse;font-size:14px">{table}</table>
  <div style="margin-top:18px;padding:14px 16px;background:#f5f6f4;border-radius:8px">
    <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#5f6672;margin-bottom:6px">Message</div>
    <div>{body}</div>
  </div>
</div>"""
    reply_to = ticket.student.email if ticket.student and ticket.student.email else None
    return subject, text, html_body, reply_to


def send_ticket_email(db: Session, ticket: SupportTicket) -> bool:
    """Email the ticket to the team and record the outcome on it. Never raises."""
    ticket.email_attempts += 1
    try:
        if (gap := not_configured()) is not None:
            ok, error = False, f"EMAIL_NOT_CONFIGURED: {gap}"
        else:
            subject, text, html_body, reply_to = compose(db, ticket)
            attachments = []
            if ticket.attachment:
                attachments.append(Attachment(ticket.attachment_name, ticket.attachment, ticket.attachment_type))
            result = get_provider().send(
                settings.support_email_to, subject, text, html_body, attachments, reply_to=reply_to,
            )
            ok, error = result.ok, result.error
    except Exception as exc:  # a broken ticket must not break the request that made it
        ok, error = False, repr(exc)

    ticket.email_status = NotificationStatus.SENT if ok else NotificationStatus.FAILED
    ticket.email_error = None if ok else error
    db.commit()
    fields = {"ticket": ticket.number, "attempt": ticket.email_attempts}
    if ok:
        log.info("support email sent", extra=fields)
    else:
        log.error("support email failed: %s", error, extra=fields)
    return ok


def retry_unsent(db: Session) -> int:
    """For the sync job: resend tickets whose email failed, for up to three days."""
    since = datetime.now(timezone.utc) - timedelta(days=3)
    pending = (
        db.query(SupportTicket)
        .filter(
            SupportTicket.email_status != NotificationStatus.SENT,
            SupportTicket.email_attempts < MAX_EMAIL_ATTEMPTS,
            SupportTicket.created_at >= since,
        )
        .order_by(SupportTicket.created_at.asc())
        .limit(20)
        .all()
    )
    return sum(send_ticket_email(db, t) for t in pending)
