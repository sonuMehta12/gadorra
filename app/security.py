import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header, HTTPException, status

from app.config import settings

ALGORITHM = "HS256"


def hash_otp(mobile: str, code: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode(), f"{mobile}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def verify_otp_hash(mobile: str, code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(mobile, code), code_hash)


def generate_otp(length: int | None = None) -> str:
    n = length or settings.otp_length
    return "".join(secrets.choice("0123456789") for _ in range(n))


def issue_form_token(mobile: str) -> tuple[str, int]:
    ttl = settings.form_token_ttl_minutes
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    payload = {"sub": mobile, "scope": "form", "exp": expires}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, ttl * 60


def decode_form_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "FORM_TOKEN_EXPIRED", "message": "Verification expired, verify the mobile again"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "FORM_TOKEN_INVALID", "message": "Invalid verification token"},
        )
    if payload.get("scope") != "form":
        raise HTTPException(status_code=401, detail={"code": "FORM_TOKEN_INVALID", "message": "Invalid token scope"})
    return payload["sub"]


def verified_mobile(x_form_token: str = Header(..., alias="X-Form-Token")) -> str:
    """FastAPI dependency: returns the OTP-verified mobile behind the request."""
    return decode_form_token(x_form_token)
