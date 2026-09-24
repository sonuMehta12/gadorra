from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session

from app.config import settings

engine = create_engine(
    settings.database_url,
    # A managed database drops idle connections, and a free service that spun
    # down wakes holding a pool full of dead ones. pre_ping tests each connection
    # before handing it out; recycle retires them before the provider does.
    pool_pre_ping=True,
    pool_recycle=280,
    pool_size=5,
    max_overflow=5,
    connect_args={"connect_timeout": 10},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
