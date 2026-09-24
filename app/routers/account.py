"""Login by WhatsApp OTP, then the student's own profile.

There is no password. "Logging in" is the same OTP flow the form uses:
/otp/send, /otp/verify, then this endpoint with the X-Form-Token it returned.
The mobile comes from the token, so a student can only ever see their own data.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Registration, RegistrationStatus, Student
from app.schemas import ProfileOut, ProfileRegistration, ProfileStudent
from app.security import verified_mobile

router = APIRouter(tags=["account"])


@router.get(
    "/me",
    response_model=ProfileOut,
    summary="The logged-in student's profile and registrations",
    description="Requires `X-Form-Token` from `/otp/verify`. Returns the student's details and "
                "every registration they have made, newest first, with the acknowledgement number "
                "and receipt links once paid. The token lasts 15 minutes; after that, verify again.",
    responses={
        404: {"description": "NOT_REGISTERED -- this mobile has never registered"},
        401: {"description": "FORM_TOKEN_INVALID or FORM_TOKEN_EXPIRED"},
        422: {"description": "FORM_TOKEN_MISSING"},
    },
)
def me(db: Session = Depends(get_db), mobile: str = Depends(verified_mobile)) -> ProfileOut:
    student = db.query(Student).filter(Student.mobile == mobile).one_or_none()
    if student is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_REGISTERED", "message": "No registration found for this mobile number"},
        )

    registrations = (
        db.query(Registration)
        .filter(Registration.student_id == student.id)
        .order_by(Registration.created_at.desc())
        .all()
    )

    items = []
    for r in registrations:
        paid = r.status is RegistrationStatus.PAID
        base = f"{settings.api_prefix}/receipts/{r.id}"
        items.append(ProfileRegistration(
            id=r.id,
            phase=r.phase.value,
            status=r.status.value,
            class_level=r.class_level,
            fee_amount_paise=r.fee_amount_paise,
            discount_paise=r.discount_paise,
            acknowledgement_number=r.acknowledgement.number if r.acknowledgement else None,
            created_at=r.created_at,
            paid_at=r.paid_at,
            receipt_url=f"{base}/view" if paid else None,
            receipt_pdf_url=f"{base}/receipt.pdf" if paid else None,
        ))

    # The first paid registration's number is the student's code -- it is the one
    # post-launch redeems for the discount, so it must not change later.
    first_paid = next((i for i in reversed(items) if i.status == "PAID" and i.acknowledgement_number), None)

    return ProfileOut(
        is_paid=first_paid is not None,
        acknowledgement_number=first_paid.acknowledgement_number if first_paid else None,
        student=ProfileStudent(
            full_name=student.full_name,
            father_name=student.father_name,
            mobile=student.mobile,
            email=student.email,
            class_level=student.class_level,
            stream=student.stream,
            district_id=student.district_id,
            district_name=student.district.name,
            address_line=student.address_line,
        ),
        registrations=items,
    )
