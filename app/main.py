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

@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if settings.sync_enabled:
        task = asyncio.create_task(sync_job.run_forever())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app = FastAPI(
    title="Gradorra GPET API",
    version="0.1.0",
    description="Student registration, payment and WhatsApp acknowledgement API.",
    lifespan=lifespan,
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
