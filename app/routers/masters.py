from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import District
from app.schemas import DistrictOut, PhaseConfigOut
from app.seed_data import CLASS_LEVELS, STREAMS

router = APIRouter(tags=["masters"])


@router.get("/masters/districts", response_model=list[DistrictOut],
            summary="All 75 districts for the dropdown")
def districts(db: Session = Depends(get_db)) -> list[District]:
    return db.query(District).filter(District.active.is_(True)).order_by(District.name).all()


@router.get("/masters/classes", summary="Class and stream options")
def classes() -> dict:
    return {
        "classes": CLASS_LEVELS,
        "streams": STREAMS,
        "stream_required_for": [11, 12],
    }


@router.get(
    "/config/phase",
    response_model=PhaseConfigOut,
    summary="Current phase, fee and the Razorpay public key",
    description="Call this on page load. `razorpay_key_id` is the public key for Checkout; "
                "the secret never leaves the server.",
)
def phase_config() -> PhaseConfigOut:
    is_pre = settings.current_phase == "PRE_LAUNCH"
    return PhaseConfigOut(
        phase=settings.current_phase,
        fee_paise=settings.fee_prelaunch_paise if is_pre else settings.fee_postlaunch_paise,
        fee_discounted_paise=(
            settings.fee_prelaunch_paise if is_pre else settings.fee_postlaunch_discounted_paise
        ),
        razorpay_key_id=settings.razorpay_key_id or None,
        razorpay_configured=settings.razorpay_configured,
    )
