"""The student portal's "Need assistance or profile correction?" form."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as ReceivedFile

from app.config import settings
from app.database import get_db
from app.logging_setup import mask_mobile
from app.schemas import SupportCategoryOut, SupportTicketOut
from app.security import verified_mobile
from app.services import support

router = APIRouter(prefix="/support", tags=["support"])

DESCRIPTION_MIN, DESCRIPTION_MAX = 10, 2000


def _invalid(field: str, problem: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "VALIDATION_ERROR", "message": "Please correct the highlighted fields",
                "fields": {field: problem}},
    )


@router.get("/categories", response_model=list[SupportCategoryOut],
            summary="Options for the issue category dropdown")
def categories() -> list[SupportCategoryOut]:
    return [SupportCategoryOut(value=v, label=label) for v, label in support.CATEGORIES.items()]


@router.post(
    "/tickets",
    response_model=SupportTicketOut,
    status_code=201,
    summary="Send a support request to the GPET team",
    description="`multipart/form-data` with `category`, `description` and an optional "
                "`attachment` (PDF, JPG, PNG or WebP). Requires `X-Form-Token`. The student's "
                "name, mobile, email and acknowledgement number are added from their record, "
                "so the form does not ask for them. The request is saved first and emailed to "
                "the team; if the email fails it is retried automatically.",
    responses={
        401: {"description": "FORM_TOKEN_INVALID, FORM_TOKEN_EXPIRED or FORM_TOKEN_REVOKED"},
        413: {"description": "ATTACHMENT_TOO_LARGE"},
        415: {"description": "ATTACHMENT_TYPE_NOT_ALLOWED -- only PDF, JPG, PNG or WebP"},
        422: {"description": "VALIDATION_ERROR (see `fields`) or FORM_TOKEN_MISSING"},
        429: {"description": "SUPPORT_RATE_LIMITED -- too many requests from this mobile today"},
    },
)
async def create_ticket(
    category: str = Form(..., description="One `value` from GET /support/categories"),
    description: str = Form(..., description=f"{DESCRIPTION_MIN} to {DESCRIPTION_MAX} characters"),
    # str: a browser submitting the form with no file chosen sends an empty, nameless
    # part, which arrives as "" rather than a file -- that means no attachment.
    attachment: UploadFile | str | None = File(None, description="Optional; PDF, JPG, PNG or WebP"),
    mobile: str = Depends(verified_mobile),
    db: Session = Depends(get_db),
) -> SupportTicketOut:
    if category not in support.CATEGORIES:
        raise _invalid("category", "choose one of the listed categories")
    description = description.strip()
    if len(description) < DESCRIPTION_MIN:
        raise _invalid("description", f"write at least {DESCRIPTION_MIN} characters")
    if len(description) > DESCRIPTION_MAX:
        raise _invalid("description", f"keep it under {DESCRIPTION_MAX} characters")

    if support.sent_today(db, mobile) >= settings.support_max_per_day:
        raise HTTPException(
            status_code=429,
            detail={"code": "SUPPORT_RATE_LIMITED",
                    "message": "You have sent several requests today. Our team will get back to you; "
                               "please try again tomorrow if you still need help."},
        )

    file = None
    if isinstance(attachment, ReceivedFile):  # the parsed form hands over Starlette's class
        limit = settings.support_attachment_max_mb * 1024 * 1024
        content = await attachment.read(limit + 1)
        if content:  # a browser sends an empty part when no file was chosen
            if len(content) > limit:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "ATTACHMENT_TOO_LARGE",
                            "message": f"The file is larger than {settings.support_attachment_max_mb} MB.",
                            "fields": {"attachment": f"maximum {settings.support_attachment_max_mb} MB"}},
                )
            kind = support.detect_type(content)
            if kind is None:
                raise HTTPException(
                    status_code=415,
                    detail={"code": "ATTACHMENT_TYPE_NOT_ALLOWED",
                            "message": "Attach a PDF or an image (JPG, PNG or WebP).",
                            "fields": {"attachment": "PDF, JPG, PNG or WebP only"}},
                )
            mime, ext = kind
            file = (support.safe_filename(attachment.filename, ext), mime, content)

    ticket = support.create_ticket(db, mobile, category, description, file)
    support.send_ticket_email(db, ticket)  # a failure is recorded and retried, not shown

    reach = mask_mobile(mobile)
    return SupportTicketOut(
        ticket_number=ticket.number,
        category=category,
        message=f"We have received your request. Your ticket number is {ticket.number}. "
                f"Our team will contact you on {reach}.",
    )
