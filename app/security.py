import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db

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
    # jti lets one token be revoked on logout without touching any other session
    payload = {"sub": mobile, "scope": "form", "exp": expires, "jti": secrets.token_hex(16)}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, ttl * 60


def decode_form_token_claims(token: str) -> dict:
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
    return payload


def decode_form_token(token: str) -> str:
    return decode_form_token_claims(token)["sub"]


def is_revoked(db: Session, jti: str | None) -> bool:
    # Tokens issued before logout existed carry no jti; they simply run out at
    # their 15-minute expiry, as they always did.
    if not jti:
        return False
    from app.models import RevokedToken
    return db.get(RevokedToken, jti) is not None


def verified_claims(
    x_form_token: str = Header(..., alias="X-Form-Token"),
    db: Session = Depends(get_db),
) -> dict:
    claims = decode_form_token_claims(x_form_token)
    if is_revoked(db, claims.get("jti")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "FORM_TOKEN_REVOKED", "message": "You have logged out. Verify your mobile to continue."},
        )
    return claims


def verified_mobile(claims: dict = Depends(verified_claims)) -> str:
    """FastAPI dependency: returns the OTP-verified mobile behind the request."""
    return claims["sub"]
