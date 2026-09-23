"""WhatsApp sending, behind one interface.

Three providers so the build never waits on Meta approvals:
  console          -> logs the message (local dev)
  meta             -> WhatsApp Cloud API (template + free-form text)

Switch with WHATSAPP_PROVIDER in .env. Nothing else in the codebase changes.
"""
import logging
from dataclasses import dataclass

import httpx

from app.config import settings

log = logging.getLogger("whatsapp")


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None


class WhatsAppProvider:
    def send_template(self, to: str, template: str, body_params: list[str], lang: str | None = None) -> SendResult:
        raise NotImplementedError

    def send_authentication(self, to: str, template: str, code: str, lang: str | None = None) -> SendResult:
        raise NotImplementedError

    def send_text(self, to: str, body: str) -> SendResult:
        raise NotImplementedError


class ConsoleProvider(WhatsAppProvider):
    def send_template(self, to: str, template: str, body_params: list[str], lang: str | None = None) -> SendResult:
        log.warning("[whatsapp:console] template=%s to=%s params=%s", template, to, body_params)
        return SendResult(ok=True, provider_message_id="console-template")

    def send_authentication(self, to: str, template: str, code: str, lang: str | None = None) -> SendResult:
        log.warning("[whatsapp:console] auth template=%s to=%s code=%s", template, to, code)
        return SendResult(ok=True, provider_message_id="console-auth")

    def send_text(self, to: str, body: str) -> SendResult:
        log.warning("[whatsapp:console] text to=%s body=%s", to, body)
        return SendResult(ok=True, provider_message_id="console-text")


class MetaCloudProvider(WhatsAppProvider):
    def __init__(self) -> None:
        self.base = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{settings.whatsapp_phone_number_id}/messages"
        )
        self.headers = {
            "Authorization": f"Bearer {settings.whatsapp_token}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict) -> SendResult:
        if not settings.whatsapp_token:
            return SendResult(ok=False, error="WHATSAPP_TOKEN is empty")
        try:
            with httpx.Client(timeout=20) as client:
                r = client.post(self.base, headers=self.headers, json=payload)
            data = r.json()
            if r.status_code >= 400:
                return SendResult(ok=False, error=str(data))
            msg_id = (data.get("messages") or [{}])[0].get("id")
            return SendResult(ok=True, provider_message_id=msg_id)
        except Exception as exc:  # network, timeout, bad json
            return SendResult(ok=False, error=repr(exc))

    def send_template(self, to: str, template: str, body_params: list[str], lang: str | None = None) -> SendResult:
        lang = lang or settings.whatsapp_template_language
        components = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(p)} for p in body_params],
                }
            )
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {"name": template, "language": {"code": lang}, "components": components},
            }
        )

    def send_authentication(self, to: str, template: str, code: str, lang: str | None = None) -> SendResult:
        """Authentication templates carry the code in the body. Some copy-code
        templates also want it echoed in a button component -- enable that with
        WHATSAPP_AUTH_TEMPLATE_BUTTON if Meta complains about the parameter count."""
        lang = lang or settings.whatsapp_template_language
        components: list[dict] = [
            {"type": "body", "parameters": [{"type": "text", "text": code}]}
        ]
        if settings.whatsapp_auth_template_button:
            components.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": code}],
                }
            )
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {"name": template, "language": {"code": lang}, "components": components},
            }
        )

    def send_text(self, to: str, body: str) -> SendResult:
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": body},
            }
        )


def get_provider() -> WhatsAppProvider:
    if settings.whatsapp_provider == "meta":
        return MetaCloudProvider()
    return ConsoleProvider()


def to_e164(mobile: str) -> str:
    """10-digit Indian mobile -> E.164 without the plus, as Meta expects."""
    digits = "".join(c for c in mobile if c.isdigit())
    if len(digits) == 10:
        return f"91{digits}"
    return digits
