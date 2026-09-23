import re

from app.config import settings
from app.services.acknowledgement import build_number, create_for_registration


def test_the_format_is_examcode_slash_state_district_slash_serial(paid):
    _, _, _, result = paid
    number = result.acknowledgement.number
    assert re.fullmatch(r"GPET26/UP\d{2}/\d{5}", number), number


def test_the_district_code_is_the_students_district(db, paid):
    _, _, _, result = paid
    district = result.registration.student.district
    assert f"/UP{district.code}/" in result.acknowledgement.number


def test_generating_twice_returns_the_same_row(db, paid):
    _, _, _, result = paid
    again = create_for_registration(db, result.registration)
    assert again.id == result.acknowledgement.id


def test_the_number_is_unique_across_registrations(db, client, verified):
    numbers = set()
    import uuid as _uuid
    from app.models import Payment, PaymentStatus
    from app.services.payments import apply_payment

    for i in range(5):
        _, headers = verified(f"98333000{i:02d}")
        reg = client.post("/api/v1/registrations", headers=headers, json={
            "full_name": f"Student {i}", "father_name": "Father",
            "class_level": 9, "district_id": 1,
            "address_line": "Somewhere in Agra", "consent_whatsapp": True,
        }).json()
        order_id = f"order_T{_uuid.uuid4().hex[:12]}"
        db.add(Payment(registration_id=_uuid.UUID(reg["id"]), razorpay_order_id=order_id,
                       amount_paise=reg["fee_amount_paise"], status=PaymentStatus.CREATED))
        db.flush()
        pid = f"pay_T{_uuid.uuid4().hex[:12]}"
        r = apply_payment(db, pid, {"id": pid, "order_id": order_id, "status": "captured",
                                    "amount": reg["fee_amount_paise"], "currency": "INR"})
        numbers.add(r.acknowledgement.number)
    assert len(numbers) == 5


def test_the_format_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "ack_exam_code", "GPET27")
    monkeypatch.setattr(settings, "ack_state_prefix", "MP")
    assert build_number("07", "12345") == "GPET27/MP07/12345"
