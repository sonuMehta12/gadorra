import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.rate_limit import client_ip, limiter
from app.database import engine
from app.routers import account, lookup, masters, otp, payments, receipts, registrations, webhooks
from app.services import sync_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

def _bootstrap_database() -> None:
    """Idempotent: create anything missing, leave everything that exists alone."""
    from app.database import Base, SessionLocal
    from app.models import District
    from app.seed_data import UP_DISTRICTS

    Base.metadata.create_all(engine)
    # create_all never alters an existing table. Columns widened after the first
    # deploy are fixed here until Alembic arrives. Widening is safe on live data.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE notifications ALTER COLUMN recipient TYPE VARCHAR(254)"))
    db = SessionLocal()
    try:
        if db.query(District).count() < len(UP_DISTRICTS):
            for index, name in enumerate(UP_DISTRICTS, start=1):
                if db.query(District).filter(District.name == name).one_or_none() is None:
                    db.add(District(code=f"{index:02d}", name=name, state="Uttar Pradesh"))
            db.commit()
        logging.getLogger("bootstrap").info("database ready, %s districts", db.query(District).count())
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auto_init_db:
        try:
            _bootstrap_database()
        except Exception:
            logging.getLogger("bootstrap").exception("database bootstrap failed")

    task = None
    if settings.sync_enabled:
        task = asyncio.create_task(sync_job.run_forever())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


API_DESCRIPTION = """
Student registration, payment and WhatsApp acknowledgement for GPET 2026.

### The flow

| # | Call | What you get |
| - | ---- | ------------ |
| 1 | `POST /otp/send` | An OTP goes to that mobile on WhatsApp |
| 2 | `POST /otp/verify` | `form_token`, valid **15 minutes** |
| 3 | `POST /registrations` | The registration, with the fee the server decided |
| 4 | `POST /payments/order` | `razorpay_order_id` + the public key for Checkout |
| 5 | `POST /payments/verify` | `PAID` and the **acknowledgement number** |
| 6 | `GET /receipts/{id}/view` | A printable receipt (`/receipt.pdf` downloads it) |

Steps 3 onward need the header `X-Form-Token: <form_token>` from step 2, except
the payment and receipt calls, which are reached by the registration id.

Never send an amount. The fee is read from server config on every call, and a
captured payment whose amount does not match its order is refused.

`POST /payments/verify` is safe to call twice -- the acknowledgement number is
generated once and the WhatsApp message sent once, however many times it fires.

### Logging in later

A student who already registered logs in the same way: `POST /otp/send`, then
`POST /otp/verify`, then `GET /me` with the `X-Form-Token`. It returns their
details and every registration with its status, acknowledgement number and
receipt links. `404 NOT_REGISTERED` means the mobile has never registered.

### Authentication

There is no login. Two things stand in for it:

- **`X-Form-Token`** -- proves this browser verified that mobile by OTP. Needed by
  `/registrations` and `/acknowledgements/*`. Missing header gives `422
  FORM_TOKEN_MISSING`, a bad one `401 FORM_TOKEN_INVALID`, an old one `401
  FORM_TOKEN_EXPIRED`, one ended by `POST /logout` `401 FORM_TOKEN_REVOKED` --
  in every case, verify the mobile again.
- **The registration id** -- a UUID, unguessable, and enough on its own to read a
  registration or its receipt.

A mobile lookup only works for the mobile the token was issued for.

### Errors

Every failure, from a bad field to a rate limit to a server fault, returns the
same shape:

```json
{
  "detail": {
    "code": "VALIDATION_ERROR",
    "message": "Please correct the highlighted fields",
    "fields": { "full_name": "String should have at least 2 characters" }
  }
}
```

- `message` is written for the student -- show it as-is.
- `code` is for your logic. Branch on it, never on `message`.
- `fields` appears only when specific inputs are at fault; the key is the field
  name, so it maps straight onto the form.

### Rate limits

| Limit | Scope |
| ----- | ----- |
| 5 registrations per 10 minutes | per IP |
| 10 OTP requests per 10 minutes | per IP |
| 10 OTP requests per hour | per mobile |
| 100 requests per minute | per IP, everything under `/api/v1` |

Over the limit gives `429` with `code` `RATE_LIMITED` or `OTP_RATE_LIMITED`, and
a `Retry-After` header in seconds.

### Generating a client

`/openapi.json` is the full machine-readable schema. `npx openapi-typescript
http://<host>/openapi.json -o src/api.d.ts` gives you typed requests and
responses that cannot drift from this server.
"""

app = FastAPI(
    title="Gradorra GPET API",
    version="0.1.0",
    description=API_DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "otp", "description": "Mobile verification over WhatsApp."},
        {"name": "masters", "description": "Dropdown data and the fee for the current phase."},
        {"name": "registrations", "description": "The registration form."},
        {"name": "payments", "description": "Razorpay order creation and settlement."},
        {"name": "receipts", "description": "Receipt as JSON, a printable page, or a PDF."},
        {"name": "account", "description": "Log in with a WhatsApp OTP and read your own profile."},
        {"name": "lookup", "description": "Find an earlier paid registration to prefill and discount."},
        {"name": "webhooks", "description": "Razorpay calls these. Not for the front end."},
        {"name": "meta", "description": "Health and diagnostics."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    otp.router, masters.router, registrations.router, payments.router,
    receipts.router, lookup.router, account.router, webhooks.router,
):
    app.include_router(r, prefix=settings.api_prefix)


# path -> (bucket, limit setting, window setting). Checked in the middleware so a
# request that fails validation still counts against the limit.
_BUCKETS = [
    ("POST", "/registrations", "registrations", "rate_limit_registration", "rate_limit_registration_window_seconds"),
    ("POST", "/otp/send", "otp", "rate_limit_otp", "rate_limit_otp_window_seconds"),
]


def error_body(code: str, message: str, fields: dict[str, str] | None = None) -> dict:
    """The one error shape this API returns, whatever went wrong."""
    detail: dict = {"code": code, "message": message}
    if fields:
        detail["fields"] = fields
    return {"detail": detail}


def _too_many(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content=error_body("RATE_LIMITED", f"Too many requests. Try again in {retry_after} seconds."),
        headers={"Retry-After": str(retry_after)},
    )


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith(settings.api_prefix):
        ip = client_ip(request)
        tail = path[len(settings.api_prefix):]

        for method, suffix, bucket, limit_attr, window_attr in _BUCKETS:
            if request.method == method and tail.rstrip("/") == suffix:
                allowed, retry_after = limiter.hit(
                    f"{bucket}:{ip}", getattr(settings, limit_attr), getattr(settings, window_attr)
                )
                if not allowed:
                    return _too_many(retry_after)

        allowed, retry_after = limiter.hit(
            f"global:{ip}", settings.rate_limit_global, settings.rate_limit_global_window_seconds
        )
        if not allowed:
            return _too_many(retry_after)

    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_failed(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Turn FastAPI's array of errors into the same shape as every other error,
    with one message per field so the form can highlight each input."""
    fields: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc", []) if part not in ("body", "query", "path")]
        name = ".".join(loc) or "request"
        message = err.get("msg", "invalid value")
        fields.setdefault(name, message.removeprefix("Value error, "))

    missing_header = "X-Form-Token" in " ".join(fields)
    return JSONResponse(
        status_code=422,
        content=error_body(
            "FORM_TOKEN_MISSING" if missing_header else "VALIDATION_ERROR",
            "Verify your mobile number first" if missing_header else "Please correct the highlighted fields",
            fields,
        ),
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("api").exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content=error_body("INTERNAL_ERROR", "Something went wrong"),
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Says *why* it is unhealthy. A bare database:false sends people hunting."""
    db_ok = True
    db_error = None
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:
        db_ok = False
        logging.getLogger("health").exception("database unreachable")
        # The message can carry the host and user; keep the shape, drop the detail.
        db_error = type(exc).__name__
        reason = str(exc).lower()
        if "could not translate host name" in reason or "name or service not known" in reason:
            db_error += ": host not found -- is the database in the same region as this service?"
        elif "timeout" in reason or "timed out" in reason:
            db_error += ": connection timed out"
        elif "password" in reason or "authentication" in reason:
            db_error += ": authentication rejected"
        elif "does not exist" in reason:
            db_error += ": database or role does not exist"
        elif "ssl" in reason:
            db_error += ": TLS negotiation failed -- try sslmode=require"

    body = {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "phase": settings.current_phase,
        "otp_channel": settings.otp_channel,
        "whatsapp_provider": settings.whatsapp_provider,
        "razorpay_configured": settings.razorpay_configured,
        "sync_job": settings.sync_enabled,
        "email": (
            "off" if not settings.email_enabled
            else "console" if settings.email_provider != "smtp"
            else "smtp" if settings.smtp_password
            else "smtp, no password -- skipped"
        ),
    }
    if db_error:
        body["database_error"] = db_error
    return body


UI_DIR = Path(__file__).resolve().parent.parent / "ui"
# The test UI is edited constantly; a cached copy sends people hunting for bugs
# that were fixed an hour ago.
NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}

if UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(UI_DIR / "index.html", headers=NO_CACHE)
