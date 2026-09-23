"""Email sending, behind one interface, same shape as the WhatsApp provider.

  console -- writes the message to the log (local dev, no credentials needed)
  smtp    -- any mailbox with SMTP: Zoho, Google Workspace, Brevo, SES SMTP

Switch with EMAIL_PROVIDER. Nothing else in the codebase changes.
"""
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

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
             attachments: list[Attachment] | None = None) -> EmailResult:
        raise NotImplementedError


class ConsoleEmailProvider(EmailProvider):
    def send(self, to, subject, text, html=None, attachments=None) -> EmailResult:
        names = [a.filename for a in (attachments or [])]
        log.warning("[email:console] to=%s subject=%r attachments=%s\n%s", to, subject, names, text)
        return EmailResult(ok=True, provider_message_id="console")


class SmtpEmailProvider(EmailProvider):
    def send(self, to, subject, text, html=None, attachments=None) -> EmailResult:
        if not settings.smtp_host or not settings.email_from:
            return EmailResult(ok=False, error="SMTP_HOST or EMAIL_FROM is not configured")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{settings.email_from_name} <{settings.email_from}>" if settings.email_from_name else settings.email_from
        message["To"] = to
        if settings.email_reply_to:
            message["Reply-To"] = settings.email_reply_to
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


def get_provider() -> EmailProvider:
    if settings.email_provider == "smtp":
        return SmtpEmailProvider()
    return ConsoleEmailProvider()
