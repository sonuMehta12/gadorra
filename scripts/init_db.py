"""Create tables and load master data. Safe to re-run."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import District  # noqa: E402
from app.seed_data import UP_DISTRICTS  # noqa: E402


def main() -> None:
    Base.metadata.create_all(engine)
    print(f"tables ready: {', '.join(sorted(Base.metadata.tables))}")

    db = SessionLocal()
    try:
        created = 0
        for index, name in enumerate(UP_DISTRICTS, start=1):
            code = f"{index:02d}"
            existing = db.query(District).filter(District.name == name).one_or_none()
            if existing:
                existing.code = code
                continue
            db.add(District(code=code, name=name, state="Uttar Pradesh"))
            created += 1
        db.commit()
        total = db.query(District).count()
        print(f"districts: {total} total, {created} newly inserted")
    finally:
        db.close()


if __name__ == "__main__":
    main()
