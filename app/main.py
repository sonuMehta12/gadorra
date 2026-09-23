import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.rate_limit import client_ip, limiter
from app.database import engine
from app.routers import lookup, masters, otp, payments, receipts, registrations, webhooks
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

1. `POST /otp/send` — student enters their mobile, an OTP goes out on WhatsApp.
2. `POST /otp/verify` — returns a **form token**, valid 15 minutes.
3. `POST /registrations` — send the form with the header `X-Form-Token`.
   The fee comes back decided by the server; never send an amount.
4. `POST /payments/order` — returns a Razorpay `order_id` and the public key.
   Open Razorpay Checkout with them.
5. `POST /payments/verify` — send Checkout's three fields back. On success the
   response carries the **acknowledgement number**. Show it on screen; it is
   also sent on WhatsApp and printed on the receipt.
6. `GET /receipts/{registration_id}/view` — printable receipt.
   `.../receipt.pdf` downloads it.

### Authentication

There is no login. Two things stand in for it:

- **`X-Form-Token`** — proves this browser verified that mobile by OTP. Needed
  by `/registrations` and every `/acknowledgements/*` call. Expired token gives
  `401 FORM_TOKEN_EXPIRED`; verify the mobile again.
- **The registration UUID** — unguessable, and enough on its own to read a
  registration or its receipt.

### Errors

Failures return `{"detail": {"code": "...", "message": "...", "fields": {...}}}`.
Show `message` to the student and branch on `code`. Validation errors (422) use
FastAPI's own shape with a `loc` path per field.

### Rate limits

5 registrations and 10 OTP requests per IP per 10 minutes, 100 requests per IP
per minute overall, and 3 OTPs per mobile per hour. Over the limit gives `429`
with a `Retry-After` header.
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
    receipts.router, lookup.router, webhooks.router,
):
    app.include_router(r, prefix=settings.api_prefix)


# path -> (bucket, limit setting, window setting). Checked in the middleware so a
# request that fails validation still counts against the limit.
_BUCKETS = [
    ("POST", "/registrations", "registrations", "rate_limit_registration", "rate_limit_registration_window_seconds"),
    ("POST", "/otp/send", "otp", "rate_limit_otp", "rate_limit_otp_window_seconds"),
]


def _too_many(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "error": {"code": "RATE_LIMITED", "message": f"Too many requests. Try again in {retry_after} seconds."},
        },
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


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("api").exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": {"code": "INTERNAL_ERROR", "message": "Something went wrong"}},
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "phase": settings.current_phase,
        "otp_channel": settings.otp_channel,
        "whatsapp_provider": settings.whatsapp_provider,
        "razorpay_configured": settings.razorpay_configured,
        "sync_job": settings.sync_enabled,
    }


UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")
