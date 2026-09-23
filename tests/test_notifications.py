from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import Notification, NotificationStatus
from app.services.notifications import send_acknowledgement


def _clear(db):
    """The `paid` fixture already sent one acknowledgement; start from a clean slate."""
    db.query(Notification).delete()
    db.flush()


def test_a_student_without_consent_is_never_messaged(db, paid):
    _, _, _, result = paid
    _clear(db)
    result.registration.student.consent_whatsapp = False
    db.flush()
    n = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
    assert n.status is NotificationStatus.FAILED
    assert n.error.startswith("NO_CONSENT")


def test_two_messages_inside_the_throughput_window_are_blocked(db, paid, monkeypatch):
    _, _, _, result = paid
    _clear(db)
    monkeypatch.setattr(settings, "whatsapp_min_seconds_between_messages", 6)
    first = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
    assert first.status is NotificationStatus.SENT
    second = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
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

    n = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
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

    n = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
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

    n = send_acknowledgement(db, result.registration, "GPET26/UP49/11111")
    assert n.status is NotificationStatus.SENT
