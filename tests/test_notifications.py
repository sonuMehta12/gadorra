from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import Notification, NotificationStatus
from app.services.notifications import (
    send_acknowledgement,
    send_acknowledgement_email,
    send_acknowledgement_whatsapp,
)


def _clear(db):
    """The `paid` fixture already sent one acknowledgement; start from a clean slate."""
    db.query(Notification).delete()
    db.flush()


def test_a_student_without_consent_is_never_messaged(db, paid):
    _, _, _, result = paid
    _clear(db)
    result.registration.student.consent_whatsapp = False
    db.flush()
    n = send_acknowledgement_whatsapp(db, result.registration, "GPET26/UP49/11111")
    assert n.status is NotificationStatus.FAILED
    assert n.error.startswith("NO_CONSENT")


def test_two_messages_inside_the_throughput_window_are_blocked(db, paid, monkeypatch):
    _, _, _, result = paid
    _clear(db)
    monkeypatch.setattr(settings, "whatsapp_min_seconds_between_messages", 6)
    first = send_acknowledgement_whatsapp(db, result.registration, "GPET26/UP49/11111")
    assert first.status is NotificationStatus.SENT
    second = send_acknowledgement_whatsapp(db, result.registration, "GPET26/UP49/11111")
    assert second.status is NotificationStatus.FAILED
    assert second.error.startswith("THROUGHPUT")


def test_the_daily_unique_recipient_cap_blocks_a_new_number(db, paid, monkeypatch):
    _, _, _, result = paid
    _clear(db)
    monkeypatch.setattr(settings, "whatsapp_daily_unique_recipients", 1)
    monkeypatch.setattr(settings, "whatsapp_min_seconds_between_messages", 0)

    db.add(Notification(
        channel="whatsapp", template="x", recipient="919000000001",
        status=NotificationStatus.SENT, created_at=datetime.now(timezone.utc),
    ))
    db.flush()

    n = send_acknowledgement_whatsapp(db, result.registration, "GPET26/UP49/11111")
    assert n.status is NotificationStatus.FAILED
    assert n.error.startswith("MESSAGING_LIMIT")


def test_a_recipient_already_inside_the_window_is_not_capped_again(db, paid, monkeypatch):
    _, _, _, result = paid
    _clear(db)
    monkeypatch.setattr(settings, "whatsapp_daily_unique_recipients", 1)
    monkeypatch.setattr(settings, "whatsapp_min_seconds_between_messages", 0)
    to = "91" + result.registration.student.mobile

    db.add(Notification(
        channel="whatsapp", template="x", recipient=to,
        status=NotificationStatus.SENT, created_at=datetime.now(timezone.utc),
    ))
    db.flush()

    n = send_acknowledgement_whatsapp(db, result.registration, "GPET26/UP49/11111")
    assert n.status is NotificationStatus.SENT


def test_an_old_send_does_not_count_towards_the_cap(db, paid, monkeypatch):
    _, _, _, result = paid
    _clear(db)
    monkeypatch.setattr(settings, "whatsapp_daily_unique_recipients", 1)
    monkeypatch.setattr(settings, "whatsapp_min_seconds_between_messages", 0)

    db.add(Notification(
        channel="whatsapp", template="x", recipient="919000000002",
        status=NotificationStatus.SENT,
        created_at=datetime.now(timezone.utc) - timedelta(hours=25),
    ))
    db.flush()

    n = send_acknowledgement_whatsapp(db, result.registration, "GPET26/UP49/11111")
    assert n.status is NotificationStatus.SENT


# ---------------------------------------------------------------- email

def test_the_acknowledgement_goes_out_on_both_channels(db, paid):
    _, _, _, result = paid
    _clear(db)
    sent = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
    channels = {n.channel: n.status for n in sent}
    assert channels["email"] is NotificationStatus.SENT
    assert channels["whatsapp"] is NotificationStatus.SENT


def test_email_still_goes_out_when_whatsapp_is_blocked(db, paid, monkeypatch):
    """WhatsApp templates are in review; the number must still reach the student."""
    _, _, _, result = paid
    _clear(db)
    result.registration.student.consent_whatsapp = False
    db.flush()

    sent = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
    by_channel = {n.channel: n for n in sent}
    assert by_channel["email"].status is NotificationStatus.SENT
    assert by_channel["whatsapp"].status is NotificationStatus.FAILED


def test_a_failing_email_does_not_stop_whatsapp(db, paid, monkeypatch):
    from app.services import notifications as notif
    from app.services.email import EmailResult

    _, _, _, result = paid
    _clear(db)

    class Broken:
        def send(self, *a, **k):
            return EmailResult(ok=False, error="smtp refused")

    monkeypatch.setattr(notif, "get_email_provider", lambda: Broken())
    sent = notif.send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
    by_channel = {n.channel: n for n in sent}
    assert by_channel["email"].status is NotificationStatus.FAILED
    assert by_channel["whatsapp"].status is NotificationStatus.SENT


def test_the_email_carries_the_number_and_the_receipt(db, paid, monkeypatch):
    from app.services import notifications as notif
    from app.services.email import EmailResult

    captured = {}

    class Spy:
        def send(self, to, subject, text, html=None, attachments=None):
            captured.update(to=to, subject=subject, text=text, html=html,
                            attachments=attachments or [])
            return EmailResult(ok=True, provider_message_id="spy")

    _, _, _, result = paid
    _clear(db)
    monkeypatch.setattr(notif, "get_email_provider", lambda: Spy())
    number = result.acknowledgement.number
    notif.send_acknowledgement_email(db, result.registration, number)

    assert number in captured["subject"]
    assert number in captured["text"]
    assert number in captured["html"]
    assert captured["to"] == result.registration.student.email
    assert len(captured["attachments"]) == 1
    assert captured["attachments"][0].filename.endswith(".pdf")
    assert captured["attachments"][0].content.startswith(b"%PDF")


def test_email_is_skipped_when_disabled(db, paid, monkeypatch):
    from app.config import settings
    _, _, _, result = paid
    _clear(db)
    monkeypatch.setattr(settings, "email_enabled", False)
    assert send_acknowledgement_email(db, result.registration, "GPET26/UP49/11111") is None
