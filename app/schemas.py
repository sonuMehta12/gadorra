import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

MOBILE_RE = re.compile(r"^[6-9]\d{9}$")


class ApiError(BaseModel):
    code: str
    message: str
    fields: dict[str, str] | None = None


# ---------- otp ----------
class OtpSendIn(BaseModel):
    mobile: str

    @field_validator("mobile")
    @classmethod
    def valid_mobile(cls, v: str) -> str:
        v = "".join(c for c in v if c.isdigit())[-10:]
        if not MOBILE_RE.match(v):
            raise ValueError("enter a valid 10-digit Indian mobile number")
        return v


class OtpSendOut(BaseModel):
    sent: bool
    expires_in_seconds: int
    channel: str
    dev_code: str | None = Field(default=None, description="only returned when OTP_CHANNEL=console")


class OtpVerifyIn(OtpSendIn):
    code: str


class OtpVerifyOut(BaseModel):
    verified: bool
    form_token: str
    expires_in_seconds: int


# ---------- masters ----------
class DistrictOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str


class PhaseConfigOut(BaseModel):
    phase: str
    fee_paise: int
    fee_discounted_paise: int
    currency: str = "INR"
    razorpay_key_id: str | None = None
    razorpay_configured: bool


# ---------- registration ----------
class RegistrationIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    father_name: str = Field(min_length=2, max_length=80)
    class_level: Literal[9, 10, 11, 12]
    district_id: int
    address_line: str = Field(min_length=5, max_length=240)
    email: EmailStr
    consent_whatsapp: bool
    acknowledgement_number: str | None = None
    bot_check_token: str | None = None

    # post-launch fields, optional for now
    stream: str | None = None
    school_name: str | None = None
    guardian_mobile: str | None = None
    city: str | None = None
    pincode: str | None = None

    @field_validator("consent_whatsapp")
    @classmethod
    def must_consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("WhatsApp consent is required, it is the only channel we use")
        return v


class StudentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    full_name: str
    father_name: str | None
    mobile: str
    email: str | None
    class_level: int
    district_id: int
    address_line: str | None


class RegistrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    phase: str
    status: str
    class_level: int
    fee_amount_paise: int
    discount_paise: int
    acknowledgement_number: str | None = None
    created_at: datetime
    student: StudentOut


# ---------- payments ----------
class OrderIn(BaseModel):
    registration_id: UUID


class OrderOut(BaseModel):
    razorpay_order_id: str
    amount_paise: int
    currency: str
    razorpay_key_id: str
    registration_id: UUID


class VerifyIn(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class VerifyOut(BaseModel):
    status: str
    acknowledgement_number: str | None
    registration_id: UUID
    message: str


# ---------- lookup ----------
class LookupIn(BaseModel):
    acknowledgement_number: str | None = None
    mobile: str | None = None

    @field_validator("mobile")
    @classmethod
    def clean_mobile(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = "".join(c for c in v if c.isdigit())[-10:]
        if not MOBILE_RE.match(v):
            raise ValueError("enter a valid 10-digit Indian mobile number")
        return v


class LookupOut(BaseModel):
    found: bool
    acknowledgement_number: str | None = None
    redeemed: bool = False
    payable_fee_paise: int
    discount_paise: int
    prefill: dict | None = None
    message: str
