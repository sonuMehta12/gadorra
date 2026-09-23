"""Bot-check token verification, pluggable.

BOT_CHECK_PROVIDER:
  none       -- accept everything (local dev, and until the client picks a provider)
  turnstile  -- Cloudflare Turnstile
  recaptcha  -- Google reCAPTCHA v2/v3

The front end sends the widget's token as `bot_check_token`; we verify it with
the provider server-side. A missing secret degrades to 'none' with a loud log
rather than silently rejecting every real student.
"""
import logging

import httpx

from app.config import settings

log = logging.getLogger("bot_check")

ENDPOINTS = {
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
}


def enabled() -> bool:
    return settings.bot_check_provider in ENDPOINTS and bool(settings.bot_check_secret)


def verify(token: str | None, remote_ip: str | None = None) -> tuple[bool, str]:
    provider = settings.bot_check_provider
    if provider == "none":
        return True, "disabled"
    if provider not in ENDPOINTS:
        log.error("unknown BOT_CHECK_PROVIDER %r, letting the request through", provider)
        return True, "unknown provider"
    if not settings.bot_check_secret:
        log.error("BOT_CHECK_PROVIDER=%s but BOT_CHECK_SECRET is empty, letting the request through", provider)
        return True, "no secret configured"
    if not token:
        return False, "bot check token missing"

    data = {"secret": settings.bot_check_secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(ENDPOINTS[provider], data=data)
        body = r.json()
    except Exception as exc:
        # The provider being down must not stop registrations.
        log.error("bot check call failed, letting the request through: %r", exc)
        return True, "provider unreachable"

    if body.get("success"):
        return True, "ok"
    return False, ",".join(body.get("error-codes", [])) or "rejected"
