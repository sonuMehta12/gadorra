from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Payment, PaymentStatus, Registration
from app.services import receipt as receipt_service

router = APIRouter(prefix="/receipts", tags=["receipts"])


def _load(registration_id: UUID, db: Session) -> dict:
    registration = db.query(Registration).filter(Registration.id == registration_id).one_or_none()
    if registration is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Registration not found"})

    payment = (
        db.query(Payment)
        .filter(Payment.registration_id == registration.id, Payment.status == PaymentStatus.CAPTURED)
        .order_by(Payment.updated_at.desc())
        .first()
    )
    try:
        return receipt_service.build(registration, payment)
    except receipt_service.ReceiptUnavailable as exc:
        raise HTTPException(status_code=409, detail={"code": "NOT_PAID", "message": str(exc)})


@router.get("/{registration_id}")
def as_json(registration_id: UUID, db: Session = Depends(get_db)) -> dict:
    return _load(registration_id, db)


@router.get("/{registration_id}/view", response_class=HTMLResponse)
def as_html(registration_id: UUID, db: Session = Depends(get_db)) -> HTMLResponse:
    """Printable page. The browser's print dialog also saves it as a PDF."""
    return HTMLResponse(receipt_service.to_html(_load(registration_id, db)))


@router.get("/{registration_id}/receipt.pdf")
def as_pdf(registration_id: UUID, db: Session = Depends(get_db)) -> Response:
    data = _load(registration_id, db)
    return Response(
        content=receipt_service.to_pdf(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{data["receipt_id"]}.pdf"'},
    )
