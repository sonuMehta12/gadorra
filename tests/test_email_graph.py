"""EMAIL_PROVIDER=graph -- Microsoft 365 through the Graph API, against a fake Microsoft."""
import base64
import json

import httpx
import pytest

from app.config import settings
from app.services import email
from app.services.email import Attachment, GraphEmailProvider, not_configured


class FakeMicrosoft:
    def __init__(self, token_status=200, send_status=202):
        self.token_calls, self.sends = [], []
        self.token_status, self.send_status = token_status, send_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            self.token_calls.append((str(request.url), dict(httpx.QueryParams(request.content.decode()))))
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={
                    "error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."})
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3599})
        self.sends.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(self.send_status, text="" if self.send_status == 202 else '{"error":{"code":"ErrorAccessDenied"}}')


@pytest.fixture
def microsoft(monkeypatch):
    fake = FakeMicrosoft()
    real_client = httpx.Client
    monkeypatch.setattr(email.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(fake), **kw))
    monkeypatch.setattr(settings, "email_provider", "graph")
    monkeypatch.setattr(settings, "email_from", "info@gradorra.com")
    monkeypatch.setattr(settings, "graph_tenant_id", "tenant-123")
    monkeypatch.setattr(settings, "graph_client_id", "client-456")
    monkeypatch.setattr(settings, "graph_client_secret", "s3cret")
    GraphEmailProvider._token = None
    yield fake
    GraphEmailProvider._token = None


def test_sends_as_the_mailbox_with_an_app_token(microsoft):
    r = GraphEmailProvider().send(
        "helpdesk@gradorra.com", "[GPET Support] Payment issue", "plain", "<p>html</p>",
        [Attachment("proof.png", b"\x89PNG data", "image/png")], reply_to="student@example.com",
    )
    assert r.ok, r.error

    [(token_url, form)] = microsoft.token_calls
    assert token_url == "https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token"
    assert form == {"grant_type": "client_credentials", "client_id": "client-456",
                    "client_secret": "s3cret", "scope": "https://graph.microsoft.com/.default"}

    [(url, auth, body)] = microsoft.sends
    assert url == "https://graph.microsoft.com/v1.0/users/info@gradorra.com/sendMail"
    assert auth == "Bearer tok-1"
    m = body["message"]
    assert m["subject"] == "[GPET Support] Payment issue"
    assert m["body"] == {"contentType": "HTML", "content": "<p>html</p>"}
    assert m["toRecipients"] == [{"emailAddress": {"address": "helpdesk@gradorra.com"}}]
    assert m["replyTo"] == [{"emailAddress": {"address": "student@example.com"}}]
    [att] = m["attachments"]
    assert att["name"] == "proof.png" and base64.b64decode(att["contentBytes"]) == b"\x89PNG data"


def test_one_token_serves_many_sends(microsoft):
    for _ in range(3):
        assert GraphEmailProvider().send("helpdesk@gradorra.com", "s", "t").ok
    assert len(microsoft.token_calls) == 1 and len(microsoft.sends) == 3


def test_a_wrong_secret_is_reported_without_echoing_it(microsoft):
    microsoft.token_status = 401
    r = GraphEmailProvider().send("helpdesk@gradorra.com", "s", "t")
    assert not r.ok
    assert "invalid_client" in r.error and "AADSTS7000215" in r.error
    assert "s3cret" not in r.error
    assert microsoft.sends == []


def test_a_refused_send_is_reported(microsoft):
    microsoft.send_status = 403  # e.g. the app may not send as this mailbox
    r = GraphEmailProvider().send("helpdesk@gradorra.com", "s", "t")
    assert not r.ok and "403" in r.error and "ErrorAccessDenied" in r.error


def test_a_rejected_token_is_dropped_so_the_next_send_fetches_a_new_one(microsoft):
    microsoft.send_status = 401
    GraphEmailProvider().send("helpdesk@gradorra.com", "s", "t")
    microsoft.send_status = 202
    assert GraphEmailProvider().send("helpdesk@gradorra.com", "s", "t").ok
    assert len(microsoft.token_calls) == 2


def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "graph")
    monkeypatch.setattr(settings, "graph_tenant_id", "t")
    monkeypatch.setattr(settings, "graph_client_id", "")
    monkeypatch.setattr(settings, "graph_client_secret", "")
    assert not_configured() == "GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET empty"
    assert isinstance(email.get_provider(), GraphEmailProvider)


def test_a_support_ticket_goes_out_through_graph(client, registration, microsoft):
    _, headers, _ = registration
    r = client.post("/api/v1/support/tickets", headers=headers,
                    data={"category": "PAYMENT", "description": "Money was deducted but no number came."})
    assert r.status_code == 201
    [(_, _, body)] = microsoft.sends
    assert body["message"]["toRecipients"][0]["emailAddress"]["address"] == settings.support_email_to
    assert r.json()["ticket_number"] in body["message"]["subject"]


def test_health_says_whether_support_mail_can_go_out(client, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "graph")
    monkeypatch.setattr(settings, "graph_client_secret", "")
    assert "not configured" in client.get("/health").json()["support_email"]
