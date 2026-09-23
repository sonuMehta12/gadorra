from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Notification, NotificationStatus, OtpRequest
from app.schemas import OtpSendIn, OtpSendOut, OtpVerifyIn, OtpVerifyOut
from app.security import generate_otp, hash_otp, issue_form_token, verify_otp_hash
from app.services.notifications import send_otp

router = APIRouter(prefix="/otp", tags=["otp"])


@router.post(
    "/send",
    response_model=OtpSendOut,
    summary="Send an OTP to a mobile number",
    responses={
        429: {"description": "OTP_TOO_SOON (wait a few seconds) or OTP_RATE_LIMITED (3 per hour reached)"},
        502: {"description": "OTP_SEND_FAILED -- WhatsApp rejected the message"},
        503: {"description": "WHATSAPP_DAILY_LIMIT -- the number's 24h recipient cap is full"},
    },
)
def send(payload: OtpSendIn, db: Session = Depends(get_db)) -> OtpSendOut:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)

    recent = (
        db.query(func.count(OtpRequest.id))
        .filter(OtpRequest.mobile == payload.mobile, OtpRequest.created_at >= window_start)
        .scalar()
    )
    if recent >= settings.otp_max_resends:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "OTP_RATE_LIMITED",
                "message": f"Too many OTP requests. Try again after an hour.",
            },
        )

    # Any earlier unconsumed OTP for this mobile is dead once a new one goes out.
    db.query(OtpRequest).filter(
        OtpRequest.mobile == payload.mobile, OtpRequest.consumed.is_(False)
    ).update({"consumed": True}, synchronize_session=False)

    code = generate_otp()
    otp = OtpRequest(
        mobile=payload.mobile,
        code_hash=hash_otp(payload.mobile, code),
        expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
    )
    db.add(otp)
    db.flush()

    notification = send_otp(db, payload.mobile, code)

    # If the send was blocked or rejected, say so instead of claiming it went out.
    if notification.status is not NotificationStatus.SENT:
        otp.consumed = True
        reason = notification.error or "the message could not be sent"
        db.commit()
        if reason.startswith("THROUGHPUT"):
            raise HTTPException(
                status_code=429,
                detail={"code": "OTP_TOO_SOON", "message": "Please wait a few seconds before requesting another OTP"},
            )
        if reason.startswith("MESSAGING_LIMIT"):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "WHATSAPP_DAILY_LIMIT",
                    "message": "We have reached today's WhatsApp limit. Please try again later.",
                },
            )
        raise HTTPException(
            status_code=502,
            detail={"code": "OTP_SEND_FAILED", "message": "Could not send the OTP on WhatsApp. Please try again."},
        )

    db.commit()

    return OtpSendOut(
        sent=True,
        expires_in_seconds=settings.otp_ttl_minutes * 60,
        channel=settings.otp_channel,
        dev_code=code if settings.otp_channel == "console" else None,
    )


@router.post(
    "/verify",
    response_model=OtpVerifyOut,
    summary="Verify the OTP and get a form token",
    description="On success returns `form_token`. Send it as the `X-Form-Token` header on "
                "`/registrations` and `/acknowledgements/*`. It lasts 15 minutes.",
    responses={
        400: {"description": "OTP_INVALID, OTP_EXPIRED or OTP_NOT_FOUND"},
        429: {"description": "OTP_TOO_MANY_ATTEMPTS -- request a new code"},
    },
)
def verify(payload: OtpVerifyIn, db: Session = Depends(get_db)) -> OtpVerifyOut:
    now = datetime.now(timezone.utc)
    otp = (
        db.query(OtpRequest)
        .filter(OtpRequest.mobile == payload.mobile, OtpRequest.consumed.is_(False))
        .order_by(OtpRequest.created_at.desc())
        .with_for_update()
        .first()
    )
    if otp is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "OTP_NOT_FOUND", "message": "Request an OTP first"},
        )
    if otp.expires_at <= now:
        otp.consumed = True
        db.commit()
        raise HTTPException(
            status_code=400, detail={"code": "OTP_EXPIRED", "message": "This OTP has expired"}
        )
    if otp.attempts >= settings.otp_max_attempts:
        otp.consumed = True
        db.commit()
        raise HTTPException(
            status_code=429,
            detail={"code": "OTP_TOO_MANY_ATTEMPTS", "message": "Too many wrong attempts, request a new OTP"},
        )

    otp.attempts += 1
    if not verify_otp_hash(payload.mobile, payload.code, otp.code_hash):
        db.commit()
        raise HTTPException(
            status_code=400,
            detail={
                "code": "OTP_INVALID",
                "message": f"Wrong code. {settings.otp_max_attempts - otp.attempts} attempts left.",
            },
        )

    otp.verified = True
    otp.consumed = True
    db.commit()

    token, ttl = issue_form_token(payload.mobile)
    return OtpVerifyOut(verified=True, form_token=token, expires_in_seconds=ttl)
