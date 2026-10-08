"""Email sending, behind one interface, same shape as the WhatsApp provider.

  console -- writes the message to the log (local dev, no credentials needed)
  smtp    -- any mailbox with SMTP: Zoho, Google Workspace, Brevo, SES SMTP
  graph   -- Microsoft 365 through the Graph API, for tenants that refuse
             password SMTP. Needs an Entra ID app with Mail.Send.

Switch with EMAIL_PROVIDER. Nothing else in the codebase changes.
"""
import base64
import logging
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

from app.config import settings

log = logging.getLogger("email")


@dataclass
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/pdf"


@dataclass
class EmailResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None


class EmailProvider:
    def send(self, to: str, subject: str, text: str, html: str | None = None,
             attachments: list[Attachment] | None = None, reply_to: str | None = None) -> EmailResult:
        raise NotImplementedError


class ConsoleEmailProvider(EmailProvider):
    def send(self, to, subject, text, html=None, attachments=None, reply_to=None) -> EmailResult:
        names = [a.filename for a in (attachments or [])]
        log.warning("[email:console] to=%s subject=%r attachments=%s\n%s", to, subject, names, text)
        return EmailResult(ok=True, provider_message_id="console")


class SmtpEmailProvider(EmailProvider):
    def send(self, to, subject, text, html=None, attachments=None, reply_to=None) -> EmailResult:
        if not settings.smtp_host or not settings.email_from:
            return EmailResult(ok=False, error="SMTP_HOST or EMAIL_FROM is not configured")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{settings.email_from_name} <{settings.email_from}>" if settings.email_from_name else settings.email_from
        message["To"] = to
        # A support ticket replies to the student; everything else to the office.
        if reply_to or settings.email_reply_to:
            message["Reply-To"] = reply_to or settings.email_reply_to
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")

        for a in attachments or []:
            maintype, _, subtype = a.mime_type.partition("/")
            message.add_attachment(a.content, maintype=maintype, subtype=subtype, filename=a.filename)

        try:
            if settings.smtp_use_ssl:
                server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                          context=ssl.create_default_context(), timeout=30)
            else:
                server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            with server:
                if settings.smtp_use_tls and not settings.smtp_use_ssl:
                    server.starttls(context=ssl.create_default_context())
                if settings.smtp_user:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(message)
            return EmailResult(ok=True, provider_message_id=message.get("Message-ID") or "sent")
        except Exception as exc:
            return EmailResult(ok=False, error=repr(exc))


class GraphEmailProvider(EmailProvider):
    """POST /users/{EMAIL_FROM}/sendMail with an app-only token."""

    TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"

    # One token serves every send until shortly before it expires (about an hour).
    _token: str | None = None
    _token_expires: float = 0.0
    _lock = threading.Lock()

    @classmethod
    def _access_token(cls, client: httpx.Client) -> str:
        with cls._lock:
            if cls._token and time.monotonic() < cls._token_expires:
                return cls._token
            r = client.post(
                cls.TOKEN_URL.format(tenant=settings.graph_tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.graph_client_id,
                    "client_secret": settings.graph_client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            data = r.json()
            if r.status_code >= 400 or "access_token" not in data:
                # error_description names the problem (wrong secret, no consent) without echoing the secret
                raise RuntimeError(f"token request failed: {data.get('error')}: {data.get('error_description', '')[:300]}")
            cls._token = data["access_token"]
            cls._token_expires = time.monotonic() + int(data.get("expires_in", 3600)) - 120
            return cls._token

    def send(self, to, subject, text, html=None, attachments=None, reply_to=None) -> EmailResult:
        message = {
            "subject": subject,
            # Graph takes one body; the HTML one when there is one.
            "body": {"contentType": "HTML", "content": html} if html else {"contentType": "Text", "content": text},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        if reply_to or settings.email_reply_to:
            message["replyTo"] = [{"emailAddress": {"address": reply_to or settings.email_reply_to}}]
        if attachments:
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": a.filename,
                    "contentType": a.mime_type,
                    "contentBytes": base64.b64encode(a.content).decode(),
                }
                for a in attachments
            ]
        try:
            with httpx.Client(timeout=30) as client:
                token = self._access_token(client)
                r = client.post(
                    self.SEND_URL.format(sender=settings.email_from),
                    headers={"Authorization": f"Bearer {token}"},
                    json={"message": message, "saveToSentItems": True},
                )
            if r.status_code == 202:  # accepted; Graph returns no message id
                return EmailResult(ok=True, provider_message_id="graph")
            if r.status_code == 401:  # token revoked or rotated early: fetch a fresh one next time
                GraphEmailProvider._token = None
            return EmailResult(ok=False, error=f"graph {r.status_code}: {r.text[:500]}")
        except Exception as exc:
            return EmailResult(ok=False, error=repr(exc))


def not_configured() -> str | None:
    """Why the chosen provider cannot send, or None when it can. Checked before
    sending, so a known gap is recorded instead of failing on the network."""
    if settings.email_provider == "smtp" and not settings.smtp_password:
        return "SMTP_PASSWORD is empty"
    if settings.email_provider == "graph":
        missing = [name for name, value in (
            ("GRAPH_TENANT_ID", settings.graph_tenant_id),
            ("GRAPH_CLIENT_ID", settings.graph_client_id),
            ("GRAPH_CLIENT_SECRET", settings.graph_client_secret),
        ) if not value]
        if missing:
            return ", ".join(missing) + " empty"
    return None


def get_provider() -> EmailProvider:
    if settings.email_provider == "smtp":
        return SmtpEmailProvider()
    if settings.email_provider == "graph":
        return GraphEmailProvider()
    return ConsoleEmailProvider()
