from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Acknowledgement, District, Phase, Registration, RegistrationStatus, Student
from app.schemas import RegistrationIn, RegistrationOut
from app.rate_limit import client_ip
from app.security import verified_mobile
from app.services import bot_check

router = APIRouter(prefix="/registrations", tags=["registrations"])


def _current_phase() -> Phase:
    return Phase[settings.current_phase]


def resolve_fee(db: Session, acknowledgement_number: str | None) -> tuple[int, int, str | None]:
    """Fee is always decided here, on the server. Returns (fee, discount, redeemed_number)."""
    phase = _current_phase()
    if phase is Phase.PRE_LAUNCH:
        return settings.fee_prelaunch_paise, 0, None

    full = settings.fee_postlaunch_paise
    if not acknowledgement_number:
        return full, 0, None

    ack = (
        db.query(Acknowledgement)
        .filter(Acknowledgement.number == acknowledgement_number.strip().upper())
        .one_or_none()
    )
    if ack is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "ACK_NOT_FOUND", "message": "This acknowledgement number was not found"},
        )
    if ack.redeemed:
        raise HTTPException(
            status_code=409,
            detail={"code": "ACK_ALREADY_REDEEMED", "message": "This acknowledgement number has already been used"},
        )
    discounted = settings.fee_postlaunch_discounted_paise
    return discounted, full - discounted, ack.number


def _serialize(registration: Registration) -> RegistrationOut:
    return RegistrationOut(
        id=registration.id,
        phase=registration.phase.value,
        status=registration.status.value,
        class_level=registration.class_level,
        fee_amount_paise=registration.fee_amount_paise,
        discount_paise=registration.discount_paise,
        acknowledgement_number=(
            registration.acknowledgement.number if registration.acknowledgement else None
        ),
        created_at=registration.created_at,
        student=registration.student,
    )


@router.post("", response_model=RegistrationOut, status_code=201)
def create(
    payload: RegistrationIn,
    request: Request,
    db: Session = Depends(get_db),
    mobile: str = Depends(verified_mobile),
) -> RegistrationOut:
    if settings.bot_check_required or bot_check.enabled():
        ok, reason = bot_check.verify(payload.bot_check_token, client_ip(request))
        if not ok:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_CHECK_FAILED", "message": "Bot verification failed. Please refresh and try again.", "fields": {"bot_check_token": reason}},
            )

    district = db.query(District).filter(District.id == payload.district_id).one_or_none()
    if district is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "Unknown district", "fields": {"district_id": "not found"}},
        )

    phase = _current_phase()
    fee, discount, redeemed = resolve_fee(db, payload.acknowledgement_number)

    student = db.query(Student).filter(Student.mobile == mobile).with_for_update().one_or_none()
    if student is None:
        student = Student(
            full_name=payload.full_name.strip(),
            father_name=payload.father_name.strip(),
            mobile=mobile,
            email=payload.email,
            class_level=payload.class_level,
            district_id=district.id,
            address_line=payload.address_line.strip(),
            consent_whatsapp=payload.consent_whatsapp,
            stream=payload.stream,
            school_name=payload.school_name,
            guardian_mobile=payload.guardian_mobile,
            city=payload.city,
            pincode=payload.pincode,
        )
        db.add(student)
        db.flush()
    else:
        # Class can legitimately change between phases (9 -> 10 after three months).
        student.full_name = payload.full_name.strip()
        student.father_name = payload.father_name.strip()
        student.class_level = payload.class_level
        student.district_id = district.id
        student.address_line = payload.address_line.strip()
        student.consent_whatsapp = payload.consent_whatsapp
        if payload.email:
            student.email = payload.email
        for field in ("stream", "school_name", "guardian_mobile", "city", "pincode"):
            value = getattr(payload, field)
            if value:
                setattr(student, field, value)

    existing = (
        db.query(Registration)
        .filter(Registration.student_id == student.id, Registration.phase == phase)
        .one_or_none()
    )
    if existing is not None:
        if existing.status is RegistrationStatus.PAID:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ALREADY_REGISTERED",
                    "message": "This mobile number has already completed this registration",
                },
            )
        # Unpaid duplicate -> hand back the same registration instead of making another.
        existing.class_level = payload.class_level
        existing.fee_amount_paise = fee
        existing.discount_paise = discount
        existing.redeemed_acknowledgement = redeemed
        existing.status = RegistrationStatus.PENDING_PAYMENT
        existing.expires_at = datetime.now(timezone.utc) + timedelta(
            hours=settings.registration_expiry_hours
        )
        db.commit()
        db.refresh(existing)
        return _serialize(existing)

    registration = Registration(
        student_id=student.id,
        phase=phase,
        class_level=payload.class_level,
        status=RegistrationStatus.PENDING_PAYMENT,
        fee_amount_paise=fee,
        discount_paise=discount,
        redeemed_acknowledgement=redeemed,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.registration_expiry_hours),
    )
    db.add(registration)
    db.commit()
    db.refresh(registration)
    return _serialize(registration)


@router.get("/{registration_id}", response_model=RegistrationOut)
def get_one(registration_id: UUID, db: Session = Depends(get_db)) -> RegistrationOut:
    registration = db.query(Registration).filter(Registration.id == registration_id).one_or_none()
    if registration is None:
        raise HTTPException(
            status_code=404, detail={"code": "NOT_FOUND", "message": "Registration not found"}
        )
    return _serialize(registration)
