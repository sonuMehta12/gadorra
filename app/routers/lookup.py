"""Acknowledgement lookup -- by number or by mobile.

Both need an OTP-verified form token, and a mobile lookup only works for the
mobile that was verified. Without that, the GPET26/UPxx/##### format is short
enough to guess, and a valid guess would expose another student's details.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Acknowledgement, Registration, RegistrationStatus, Student
from app.schemas import LookupIn, LookupOut
from app.security import verified_mobile

router = APIRouter(prefix="/acknowledgements", tags=["lookup"])


def _prefill(student: Student) -> dict:
    return {
        "full_name": student.full_name,
        "father_name": student.father_name,
        "address_line": student.address_line,
        "mobile": student.mobile,
        "email": student.email,
        "class_level": student.class_level,
        "district_id": student.district_id,
        "district_name": student.district.name if student.district else None,
        "locked_fields": ["full_name", "father_name", "mobile", "district_id"],
        "editable_fields": ["class_level", "email", "address_line"],
    }


def _fees(discounted: bool) -> tuple[int, int]:
    full = settings.fee_postlaunch_paise
    if not discounted:
        return full, 0
    disc = settings.fee_postlaunch_discounted_paise
    return disc, full - disc


@router.post("/lookup", response_model=LookupOut)
def lookup(
    payload: LookupIn,
    db: Session = Depends(get_db),
    mobile: str = Depends(verified_mobile),
) -> LookupOut:
    if not payload.acknowledgement_number and not payload.mobile:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "Send an acknowledgement number or a mobile number"},
        )

    ack: Acknowledgement | None = None

    if payload.acknowledgement_number:
        ack = (
            db.query(Acknowledgement)
            .filter(Acknowledgement.number == payload.acknowledgement_number.strip().upper())
            .one_or_none()
        )
    else:
        if payload.mobile != mobile:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "MOBILE_NOT_VERIFIED",
                    "message": "You can only look up the mobile number you verified",
                },
            )
        student = db.query(Student).filter(Student.mobile == payload.mobile).one_or_none()
        if student is not None:
            paid = (
                db.query(Registration)
                .filter(
                    Registration.student_id == student.id,
                    Registration.status == RegistrationStatus.PAID,
                )
                .order_by(Registration.created_at.asc())
                .first()
            )
            if paid is not None:
                ack = paid.acknowledgement

    if ack is None:
        fee, discount = _fees(discounted=False)
        return LookupOut(
            found=False,
            payable_fee_paise=fee,
            discount_paise=discount,
            message="No earlier paid registration found. The full fee applies.",
        )

    if ack.redeemed:
        fee, discount = _fees(discounted=False)
        return LookupOut(
            found=True,
            acknowledgement_number=ack.number,
            redeemed=True,
            payable_fee_paise=fee,
            discount_paise=discount,
            message="This acknowledgement number has already been used. The full fee applies.",
        )

    fee, discount = _fees(discounted=True)
    student = ack.registration.student
    return LookupOut(
        found=True,
        acknowledgement_number=ack.number,
        redeemed=False,
        payable_fee_paise=fee,
        discount_paise=discount,
        prefill=_prefill(student),
        message=f"Verified. Rs {discount / 100:.0f} discount applied.",
    )


@router.post("/resend")
def resend(
    db: Session = Depends(get_db),
    mobile: str = Depends(verified_mobile),
) -> dict:
    """Student lost the number: re-send it to the mobile they just verified."""
    from app.services.notifications import send_acknowledgement

    student = db.query(Student).filter(Student.mobile == mobile).one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "No registration on this mobile"})

    paid = (
        db.query(Registration)
        .filter(Registration.student_id == student.id, Registration.status == RegistrationStatus.PAID)
        .order_by(Registration.created_at.asc())
        .first()
    )
    if paid is None or paid.acknowledgement is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "No paid registration found on this mobile"},
        )

    send_acknowledgement(db, paid, paid.acknowledgement.number)
    db.commit()
    return {"sent": True, "acknowledgement_number": paid.acknowledgement.number}
