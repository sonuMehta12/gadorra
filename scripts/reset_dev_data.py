"""Delete a student and everything hanging off them. Development only.

    python scripts/reset_dev_data.py 9650779490
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Acknowledgement, Notification, OtpRequest, Payment, Registration, Student,
)


def main(mobile: str) -> None:
    db = SessionLocal()
    try:
        student = db.query(Student).filter(Student.mobile == mobile).one_or_none()
        if student is None:
            print(f"no student on {mobile}")
            return
        regs = db.query(Registration).filter(Registration.student_id == student.id).all()
        reg_ids = [r.id for r in regs]
        if reg_ids:
            db.query(Acknowledgement).filter(Acknowledgement.registration_id.in_(reg_ids)).delete(synchronize_session=False)
            db.query(Payment).filter(Payment.registration_id.in_(reg_ids)).delete(synchronize_session=False)
            db.query(Notification).filter(Notification.registration_id.in_(reg_ids)).delete(synchronize_session=False)
        db.query(Registration).filter(Registration.student_id == student.id).delete(synchronize_session=False)
        db.query(OtpRequest).filter(OtpRequest.mobile == mobile).delete(synchronize_session=False)
        db.delete(student)
        db.commit()
        print(f"cleared {mobile}: {len(reg_ids)} registration(s) and their payments, acknowledgements, notifications")
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1])
