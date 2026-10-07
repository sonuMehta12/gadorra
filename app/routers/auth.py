"""Login for a student who has already registered.

The same WhatsApp OTP as the form, with one difference: a mobile that never
registered is turned away before any code is sent. /otp/send and /otp/verify
stay open to everyone, because a new student needs them to reach the form.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.account import build_profile, registered_student
from app.routers.otp import check_otp, issue_otp
from app.schemas import LoginOut, OtpSendIn, OtpSendOut, OtpVerifyIn
from app.security import issue_form_token

router = APIRouter(prefix="/auth/login", tags=["account"])

NOT_REGISTERED = {"description": "NOT_REGISTERED -- this mobile has never registered; send them to the form"}


@router.post(
    "/send-otp",
    response_model=OtpSendOut,
    summary="Log in, step 1: send an OTP to a registered mobile",
    description="Sends the code only if this mobile has submitted the registration form, "
                "paid or not. Otherwise `404 NOT_REGISTERED` and no message goes out.",
    responses={
        404: NOT_REGISTERED,
        429: {"description": "OTP_TOO_SOON, OTP_RATE_LIMITED or RATE_LIMITED"},
        502: {"description": "OTP_SEND_FAILED -- WhatsApp rejected the message"},
        503: {"description": "WHATSAPP_DAILY_LIMIT"},
    },
)
def send_otp(payload: OtpSendIn, db: Session = Depends(get_db)) -> OtpSendOut:
    registered_student(db, payload.mobile)
    return issue_otp(db, payload.mobile)


@router.post(
    "/verify",
    response_model=LoginOut,
    summary="Log in, step 2: verify the OTP and get the token and profile",
    description="Returns `form_token` (send it as `X-Form-Token`) and the student's profile, "
                "the same body `GET /me` returns. Call `GET /me` with the token to reload it later.",
    responses={
        400: {"description": "OTP_INVALID, OTP_EXPIRED or OTP_NOT_FOUND"},
        404: NOT_REGISTERED,
        429: {"description": "OTP_TOO_MANY_ATTEMPTS -- request a new code"},
    },
)
def verify(payload: OtpVerifyIn, db: Session = Depends(get_db)) -> LoginOut:
    # Checked before the code, so a code is never spent on a mobile that cannot log in.
    student = registered_student(db, payload.mobile)
    check_otp(db, payload.mobile, payload.code)
    token, ttl = issue_form_token(payload.mobile)
    return LoginOut(
        verified=True, form_token=token, expires_in_seconds=ttl, profile=build_profile(db, student)
    )
