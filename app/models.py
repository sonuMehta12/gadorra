import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Phase(str, enum.Enum):
    PRE_LAUNCH = "PRE_LAUNCH"
    POST_LAUNCH = "POST_LAUNCH"


class RegistrationStatus(str, enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, enum.Enum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class NotificationStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class District(Base):
    __tablename__ = "districts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(8), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(80), nullable=False, default="Uttar Pradesh")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Student(Base, TimestampMixin):
    __tablename__ = "students"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    full_name: Mapped[str] = mapped_column(String(80), nullable=False)
    father_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mobile: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    class_level: Mapped[int] = mapped_column(Integer, nullable=False)
    district_id: Mapped[int] = mapped_column(ForeignKey("districts.id"), nullable=False)
    consent_whatsapp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    address_line_captured_at_prelaunch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # reserved for post-launch, collected later
    stream: Mapped[str | None] = mapped_column(String(40), nullable=True)
    date_of_birth: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    school_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    guardian_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    guardian_mobile: Mapped[str | None] = mapped_column(String(10), nullable=True)
    address_line: Mapped[str | None] = mapped_column(String(240), nullable=True)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(6), nullable=True)

    district: Mapped[District] = relationship(lazy="selectin")
    registrations: Mapped[list["Registration"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )


class Registration(Base, TimestampMixin):
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("student_id", "phase", name="uq_registration_student_phase"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id"), nullable=False, index=True)
    phase: Mapped[Phase] = mapped_column(Enum(Phase, name="phase"), nullable=False)
    class_level: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RegistrationStatus] = mapped_column(
        Enum(RegistrationStatus, name="registration_status"),
        nullable=False,
        default=RegistrationStatus.PENDING_PAYMENT,
        index=True,
    )
    fee_amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_paise: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    redeemed_acknowledgement: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    student: Mapped[Student] = relationship(back_populates="registrations", lazy="selectin")
    payments: Mapped[list["Payment"]] = relationship(back_populates="registration")
    acknowledgement: Mapped["Acknowledgement | None"] = relationship(
        back_populates="registration",
        uselist=False,
        foreign_keys="Acknowledgement.registration_id",
    )


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    registration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registrations.id"), nullable=False, index=True
    )
    razorpay_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), nullable=False, default=PaymentStatus.CREATED
    )
    method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    registration: Mapped[Registration] = relationship(back_populates="payments")


class Acknowledgement(Base, TimestampMixin):
    __tablename__ = "acknowledgements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    registration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registrations.id"), unique=True, nullable=False
    )
    district_code: Mapped[str] = mapped_column(String(8), nullable=False)
    redeemed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    redeemed_by_registration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registrations.id"), nullable=True
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    registration: Mapped[Registration] = relationship(
        back_populates="acknowledgement", foreign_keys=[registration_id]
    )


class OtpRequest(Base, TimestampMixin):
    __tablename__ = "otp_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    mobile: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registrations.id"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="whatsapp")
    template: Mapped[str] = mapped_column(String(80), nullable=False)
    recipient: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status"),
        nullable=False,
        default=NotificationStatus.QUEUED,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WebhookEvent(Base, TimestampMixin):
    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    event_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    event_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
