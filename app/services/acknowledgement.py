"""Acknowledgement number: GPET26/UP75/48213

  GPET26  exam code + year   (config)
  UP75    state prefix + the student's district code
  48213   random, unique within that district prefix

Generated only after a captured payment, inside the same transaction that marks
the registration PAID, so one payment can only ever produce one number.
"""
import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Acknowledgement, Registration

MAX_TRIES = 12


def build_number(district_code: str, serial: str) -> str:
    return f"{settings.ack_exam_code}/{settings.ack_state_prefix}{district_code}/{serial}"


def _random_serial() -> str:
    upper = 10 ** settings.ack_digits
    return str(secrets.randbelow(upper)).zfill(settings.ack_digits)


def create_for_registration(db: Session, registration: Registration) -> Acknowledgement:
    """Idempotent: returns the existing one if this registration already has a number."""
    existing = (
        db.query(Acknowledgement)
        .filter(Acknowledgement.registration_id == registration.id)
        .one_or_none()
    )
    if existing:
        return existing

    district_code = registration.student.district.code

    for _ in range(MAX_TRIES):
        number = build_number(district_code, _random_serial())
        ack = Acknowledgement(
            number=number,
            registration_id=registration.id,
            district_code=district_code,
        )
        try:
            db.add(ack)
            db.flush()
            return ack
        except IntegrityError:
            db.rollback()
            db.add(registration)
            continue

    raise RuntimeError(
        f"could not generate a unique acknowledgement number for district {district_code} "
        f"after {MAX_TRIES} tries -- widen ACK_DIGITS"
    )
