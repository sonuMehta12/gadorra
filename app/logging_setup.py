"""Where the logs go.

Always to the terminal, as readable lines, so `docker compose logs` and the
Render dashboard work as before. When LOG_DIR is set, also to
LOG_DIR/app.log as JSON, one object per line, so a line can be appended
without rewriting the file and searched with grep or jq:

  {"ts": "2026-10-07T21:14:03+05:30", "level": "INFO", "logger": "payments",
   "msg": "registration paid", "registration_id": "...", "ack": "GPET26/UP02/31457"}

The file starts afresh at midnight UTC; the previous day becomes
app.log.2026-10-07, and days older than LOG_RETENTION_DAYS are deleted.
"""
import json
import logging
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30), "IST")

# Attributes every LogRecord has. Anything else came from `extra=` and is
# written as its own JSON field.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


# Last line of defence: an error message from Postgres, WhatsApp or SMTP can
# quote the value it choked on. Mobiles and emails are masked wherever they appear.
_MOBILE = re.compile(r"(?<!\d)(?:91)?([6-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def scrub(text: str) -> str:
    text = _MOBILE.sub(r"\1****\2", text)
    return _EMAIL.sub(r"\1***@\2", text)


class ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(self._json(record))

    def _json(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, IST).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["error"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging(log_dir: str = "", retention_days: int = 7) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Re-running (tests, reload) must not stack a second copy of every handler.
    for h in list(root.handlers):
        if getattr(h, "_gradorra", False):
            root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ScrubbingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console._gradorra = True
    root.addHandler(console)

    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        to_file = TimedRotatingFileHandler(
            path / "app.log", when="midnight", utc=True,
            backupCount=retention_days, encoding="utf-8",
        )
        to_file.setFormatter(JsonFormatter())
        to_file._gradorra = True
        root.addHandler(to_file)

    # The request log below replaces uvicorn's own access line, which only knows
    # the proxy's address, not the student's.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every outgoing WhatsApp call at INFO; the notifications table
    # already records each one, so here it is only noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def mask_mobile(mobile: str | None) -> str | None:
    """9935846948 -> 99****6948: enough to match a complaint, not enough to spam."""
    if not mobile or len(mobile) < 6:
        return mobile
    return mobile[:2] + "*" * (len(mobile) - 6) + mobile[-4:]
