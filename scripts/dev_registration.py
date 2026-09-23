"""Create a PENDING_PAYMENT registration without going through OTP.

Development helper only -- lets you jump straight to the checkout step while
the real OTP is going to a phone you may not have in hand.

    python scripts/dev_registration.py "Name" "Father" 10 49 9123456780
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Phase, Registration, RegistrationStatus, Student  # noqa: E402


def main(name: str, father: str, klass: int, district_id: int, mobile: str) -> None:
    db = SessionLocal()
    try:
        student = db.query(Student).filter(Student.mobile == mobile).one_or_none()
        if student is None:
            student = Student(
                full_name=name, father_name=father, mobile=mobile, class_level=klass,
                district_id=district_id, address_line="Dev address, Lucknow",
                consent_whatsapp=True,
            )
            db.add(student)
            db.flush()

        phase = Phase[settings.current_phase]
        reg = (
            db.query(Registration)
            .filter(Registration.student_id == student.id, Registration.phase == phase)
            .one_or_none()
        )
        if reg is None:
            reg = Registration(
                student_id=student.id, phase=phase, class_level=klass,
                status=RegistrationStatus.PENDING_PAYMENT,
                fee_amount_paise=settings.fee_prelaunch_paise,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
            )
            db.add(reg)
        else:
            reg.status = RegistrationStatus.PENDING_PAYMENT
        db.commit()
        print(reg.id)
    finally:
        db.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], a[1], int(a[2]), int(a[3]), a[4])
