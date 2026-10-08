"""POST /support/tickets -- the portal's support form, emailed to the team."""
import pytest

from app.config import settings
from app.models import NotificationStatus, SupportTicket
from app.services import support
from app.services.email import EmailResult

URL = "/api/v1/support/tickets"
PDF = b"%PDF-1.7\n" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


class Outbox:
    """Stands in for the email provider and keeps what it was asked to send."""
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, to, subject, text, html=None, attachments=None, reply_to=None):
        self.sent.append(dict(to=to, subject=subject, text=text, html=html,
                              attachments=attachments or [], reply_to=reply_to))
        return EmailResult(ok=self.ok, provider_message_id="test" if self.ok else None,
                           error=None if self.ok else "SMTPAuthenticationError(535)")


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(support, "get_provider", lambda: box)
    return box


def _send(client, headers, category="PAYMENT", description="Money was deducted but no number came.", files=None):
    return client.post(URL, headers=headers, data={"category": category, "description": description}, files=files)


def test_the_dropdown_options_come_from_the_api(client):
    body = client.get("/api/v1/support/categories").json()
    values = [c["value"] for c in body]
    assert "PROFILE_CORRECTION" in values and "OTHER" in values
    assert all(c["label"] for c in body)


def test_a_token_is_required(client, outbox):
    r = client.post(URL, data={"category": "PAYMENT", "description": "Money was deducted."})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "FORM_TOKEN_MISSING"
    assert outbox.sent == []


def test_a_paid_student_reaches_the_team_with_their_details_filled_in(client, db, paid, outbox):
    _, headers, mobile, result = paid
    r = _send(client, headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ticket_number"].startswith("SUP-")
    assert body["ticket_number"] in body["message"]

    [mail] = outbox.sent
    assert mail["to"] == settings.support_email_to == "helpdesk@gradorra.com"
    assert body["ticket_number"] in mail["subject"] and "Test Student" in mail["subject"]
    for expected in ("Test Student", "Test Father", mobile, result.acknowledgement.number,
                     "Payment issue", "Lucknow", "Money was deducted but no number came."):
        assert expected in mail["text"], expected
    assert mail["reply_to"] == "mohammad.student.long.address@example.com"  # the team replies to the student

    ticket = db.query(SupportTicket).filter(SupportTicket.number == body["ticket_number"]).one()
    assert ticket.email_status is NotificationStatus.SENT and ticket.mobile == mobile


def test_a_verified_but_unregistered_mobile_can_still_ask_for_help(client, verified, outbox):
    # e.g. a student who paid but whose registration cannot be found
    mobile, headers = verified("9877700001")
    r = _send(client, headers, category="LOGIN_OTP", description="I paid but login says not registered.")
    assert r.status_code == 201
    [mail] = outbox.sent
    assert mobile in mail["text"] and "has not submitted the form" in mail["text"]
    assert mail["reply_to"] is None


def test_an_unknown_category_is_refused(client, registration, outbox):
    _, headers, _ = registration
    r = _send(client, headers, category="REFUND_ME_NOW")
    assert r.status_code == 422
    assert "category" in r.json()["detail"]["fields"]


@pytest.mark.parametrize("text,problem", [("help", "at least"), ("x" * 2001, "under")])
def test_the_description_must_be_a_sensible_length(client, registration, outbox, text, problem):
    _, headers, _ = registration
    r = _send(client, headers, description=text)
    assert r.status_code == 422
    assert problem in r.json()["detail"]["fields"]["description"]
    assert outbox.sent == []


def test_a_pdf_is_attached_to_the_email(client, registration, outbox):
    _, headers, _ = registration
    r = _send(client, headers, files={"attachment": ("../../etc/marksheet.pdf", PDF, "application/pdf")})
    assert r.status_code == 201
    [att] = outbox.sent[0]["attachments"]
    assert att.filename == "marksheet.pdf" and att.mime_type == "application/pdf" and att.content == PDF


def test_the_type_is_read_from_the_file_not_its_name(client, registration, outbox):
    _, headers, _ = registration
    # a PNG named .pdf is attached as the PNG it is
    _send(client, headers, files={"attachment": ("photo.pdf", PNG, "application/pdf")})
    assert outbox.sent[0]["attachments"][0].filename == "photo.png"
    # a program named .pdf is refused
    r = _send(client, headers, files={"attachment": ("form.pdf", b"MZ\x90\x00 program", "application/pdf")})
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "ATTACHMENT_TYPE_NOT_ALLOWED"


def test_a_file_over_the_limit_is_refused(client, registration, outbox, monkeypatch):
    monkeypatch.setattr(settings, "support_attachment_max_mb", 1)
    _, headers, _ = registration
    big = b"%PDF-" + b"0" * (1024 * 1024)
    r = _send(client, headers, files={"attachment": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "ATTACHMENT_TOO_LARGE"
    assert outbox.sent == []


def test_an_empty_file_field_counts_as_no_attachment(client, registration, outbox):
    _, headers, _ = registration
    r = _send(client, headers, files={"attachment": ("", b"", "application/octet-stream")})
    assert r.status_code == 201
    assert outbox.sent[0]["attachments"] == []


def test_what_the_student_typed_cannot_inject_html_into_the_email(client, registration, outbox):
    _, headers, _ = registration
    _send(client, headers, description='Please fix <script>alert(1)</script> my <a href="x">name</a>')
    html_body = outbox.sent[0]["html"]
    assert "<script>" not in html_body and "&lt;script&gt;" in html_body


def test_one_mobile_cannot_flood_the_inbox(client, registration, outbox, monkeypatch):
    monkeypatch.setattr(settings, "support_max_per_day", 2)
    _, headers, _ = registration
    assert _send(client, headers).status_code == 201
    assert _send(client, headers).status_code == 201
    r = _send(client, headers)
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "SUPPORT_RATE_LIMITED"
    assert len(outbox.sent) == 2


def test_a_failed_email_keeps_the_ticket_and_is_retried(client, db, registration, monkeypatch):
    _, headers, _ = registration
    down = Outbox(ok=False)
    monkeypatch.setattr(support, "get_provider", lambda: down)
    r = _send(client, headers)
    assert r.status_code == 201  # the student is not told to try again: it is saved
    ticket = db.query(SupportTicket).filter(SupportTicket.number == r.json()["ticket_number"]).one()
    assert ticket.email_status is NotificationStatus.FAILED and "535" in ticket.email_error

    up = Outbox(ok=True)
    monkeypatch.setattr(support, "get_provider", lambda: up)
    assert support.retry_unsent(db) == 1
    assert ticket.email_status is NotificationStatus.SENT and ticket.email_attempts == 2
    assert support.retry_unsent(db) == 0  # sent once, never again


def test_without_an_smtp_password_nothing_is_attempted(client, db, registration, outbox, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "smtp_password", "")
    _, headers, _ = registration
    r = _send(client, headers)
    assert r.status_code == 201
    ticket = db.query(SupportTicket).filter(SupportTicket.number == r.json()["ticket_number"]).one()
    assert ticket.email_error.startswith("EMAIL_NOT_CONFIGURED")
    assert outbox.sent == []


def test_the_new_codes_are_documented(client):
    text = str(client.get("/openapi.json").json())
    for code in ("SUPPORT_RATE_LIMITED", "ATTACHMENT_TOO_LARGE", "ATTACHMENT_TYPE_NOT_ALLOWED"):
        assert code in text
