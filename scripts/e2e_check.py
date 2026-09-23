"""End-to-end check of the whole registration chain against a running server.

Uses the console OTP channel so it needs no phone in hand; everything else --
Razorpay order creation, acknowledgement generation, receipt, lookup -- runs
against the real services.

    python scripts/e2e_check.py
"""
import os
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
API = f"{BASE}/api/v1"

passed, failed = [], []


def check(label: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}")


def main() -> None:
    from app.config import Settings
    from app.database import SessionLocal
    from app.models import OtpRequest, Registration, RegistrationStatus
    from app.security import hash_otp
    from app.services.payments import apply_payment
    from app.models import Payment, PaymentStatus

    s = Settings()
    mobile = "9" + str(uuid.uuid4().int)[:9]
    c = httpx.Client(timeout=30)

    print(f"\n--- health ---")
    h = c.get(f"{BASE}/health").json()
    check("server healthy", h["status"] == "ok", f"db={h['database']}")
    check("sync job running", h["sync_job"] is True)

    print(f"\n--- masters ---")
    districts = c.get(f"{API}/masters/districts").json()
    check("75 districts", len(districts) == 75, f"got {len(districts)}")
    cfg = c.get(f"{API}/config/phase").json()
    check("razorpay in test mode", cfg["razorpay_key_id"].startswith("rzp_test"), cfg["razorpay_key_id"][:12])
    check("fee is Rs 99", cfg["fee_paise"] == 9900, str(cfg["fee_paise"]))

    print(f"\n--- otp ---")
    # force console for this run so no WhatsApp message is spent on a fake number
    db = SessionLocal()
    r = c.post(f"{API}/otp/send", json={"mobile": mobile})
    if r.status_code != 200:
        check("otp send", False, r.text[:160])
        print("\nSet OTP_CHANNEL=console in .env to run this check without a real phone.")
        db.close()
        summary()
        return
    check("otp send", True, f"channel={r.json()['channel']}")

    code = r.json().get("dev_code")
    if not code:
        # console channel is off; read nothing from the DB (only the hash is stored)
        check("otp code available to the test", False, "OTP_CHANNEL is not console, cannot continue unattended")
        db.close()
        summary()
        return

    v = c.post(f"{API}/otp/verify", json={"mobile": mobile, "code": code})
    check("otp verify", v.status_code == 200, v.text[:120])
    token = v.json()["form_token"]
    headers = {"X-Form-Token": token}

    bad = c.post(f"{API}/otp/verify", json={"mobile": mobile, "code": "000000"})
    check("wrong otp rejected", bad.status_code >= 400)

    print(f"\n--- registration ---")
    payload = {
        "full_name": "E2E Student", "father_name": "E2E Father",
        "class_level": 11, "district_id": 49,
        "address_line": "1 Test Road, Lucknow", "consent_whatsapp": True,
    }
    no_token = c.post(f"{API}/registrations", json=payload)
    check("registration needs a verified mobile", no_token.status_code >= 400, str(no_token.status_code))

    reg_r = c.post(f"{API}/registrations", json=payload, headers=headers)
    check("registration created", reg_r.status_code == 201, reg_r.text[:160])
    reg = reg_r.json()
    reg_id = reg["id"]
    check("fee decided server-side", reg["fee_amount_paise"] == 9900, str(reg["fee_amount_paise"]))
    check("father name stored", reg["student"]["father_name"] == "E2E Father")

    dup = c.post(f"{API}/registrations", json=payload, headers=headers)
    check("duplicate returns the same registration", dup.json().get("id") == reg_id)

    print(f"\n--- payment ---")
    order = c.post(f"{API}/payments/order", json={"registration_id": reg_id})
    check("razorpay order created", order.status_code == 200, order.text[:160])
    order_id = order.json()["razorpay_order_id"]
    check("order id looks real", order_id.startswith("order_"), order_id)
    check("order amount matches the fee", order.json()["amount_paise"] == 9900)

    # capture is simulated: completing a real checkout needs a human at the modal
    payment_id = f"pay_E2E{uuid.uuid4().hex[:11]}"
    fake = {"id": payment_id, "order_id": order_id, "status": "captured",
            "amount": 9900, "currency": "INR", "method": "upi", "simulated": True}
    result = apply_payment(db, payment_id, fake)
    db.commit()
    check("registration marked PAID", result.registration.status is RegistrationStatus.PAID)
    ack = result.acknowledgement.number
    check("acknowledgement issued", bool(ack), ack)
    check("acknowledgement format", ack.startswith(f"{s.ack_exam_code}/{s.ack_state_prefix}") and len(ack.rsplit("/", 1)[-1]) == s.ack_digits, ack)

    again = apply_payment(db, payment_id, fake)
    db.commit()
    check("replay is idempotent", again.acknowledgement.number == ack and not again.newly_paid)

    print(f"\n--- receipt ---")
    rj = c.get(f"{API}/receipts/{reg_id}")
    check("receipt json", rj.status_code == 200, rj.text[:120])
    check("receipt id derived from ack", rj.json()["receipt_id"].endswith(ack.rsplit("/", 1)[-1]), rj.json()["receipt_id"])
    rh = c.get(f"{API}/receipts/{reg_id}/view")
    check("receipt html", rh.status_code == 200 and "<html" in rh.text.lower())
    rp = c.get(f"{API}/receipts/{reg_id}/receipt.pdf")
    check("receipt pdf", rp.status_code == 200 and rp.content[:4] == b"%PDF", f"{len(rp.content)} bytes")

    print(f"\n--- lookup ---")
    lk = c.post(f"{API}/acknowledgements/lookup", json={"acknowledgement_number": ack}, headers=headers)
    check("lookup by ack number", lk.status_code == 200 and lk.json()["found"] is True)
    check("discount applied", lk.json()["payable_fee_paise"] == s.fee_postlaunch_discounted_paise, str(lk.json()["payable_fee_paise"]))
    lm = c.post(f"{API}/acknowledgements/lookup", json={"mobile": mobile}, headers=headers)
    check("lookup by verified mobile", lm.status_code == 200 and lm.json()["found"] is True)
    other = c.post(f"{API}/acknowledgements/lookup", json={"mobile": "9000000001"}, headers=headers)
    check("someone else's mobile blocked", other.status_code == 403)
    anon = c.post(f"{API}/acknowledgements/lookup", json={"acknowledgement_number": ack})
    check("lookup needs a token", anon.status_code >= 400, str(anon.status_code))

    # clean up the row this run created
    from app.models import Acknowledgement, Notification, Student
    reg_row = db.query(Registration).filter(Registration.id == uuid.UUID(reg_id)).one()
    db.query(Acknowledgement).filter(Acknowledgement.registration_id == reg_row.id).delete()
    db.query(Payment).filter(Payment.registration_id == reg_row.id).delete()
    db.query(Notification).filter(Notification.registration_id == reg_row.id).delete()
    student_id = reg_row.student_id
    db.query(Registration).filter(Registration.id == reg_row.id).delete()
    db.query(OtpRequest).filter(OtpRequest.mobile == mobile).delete()
    db.flush()
    db.query(Student).filter(Student.id == student_id).delete()
    db.commit()
    db.close()
    print("\n(test rows removed)")
    summary()


def summary() -> None:
    print("\n" + "=" * 46)
    print(f"  {len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"    FAILED: {f}")
    print("=" * 46)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
