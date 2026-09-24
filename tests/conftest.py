"""Test setup.

Runs against a separate `gradorra_test` database so a test run can never touch
development data. Each test gets a transaction that is rolled back afterwards.
WhatsApp and Razorpay are stubbed -- no network call leaves the machine.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before app.config is imported anywhere.
DEV_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://gradorra:gradorra@localhost:5435/gradorra")
TEST_URL = DEV_URL.rsplit("/", 1)[0] + "/gradorra_test"
os.environ["DATABASE_URL"] = TEST_URL
os.environ["OTP_CHANNEL"] = "console"
os.environ["WHATSAPP_PROVIDER"] = "console"
os.environ["EMAIL_PROVIDER"] = "console"   # never open a real SMTP socket in tests
os.environ["EMAIL_ENABLED"] = "true"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_dummy"
os.environ["RAZORPAY_KEY_SECRET"] = "dummysecret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "whsecret"
os.environ["CURRENT_PHASE"] = "PRE_LAUNCH"
os.environ["RATE_LIMIT_REGISTRATION"] = "1000"
os.environ["RATE_LIMIT_OTP"] = "1000"
os.environ["RATE_LIMIT_GLOBAL"] = "100000"
os.environ["WHATSAPP_MIN_SECONDS_BETWEEN_MESSAGES"] = "0"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _ensure_database() -> None:
    admin = DEV_URL.replace("postgresql+psycopg://", "postgresql://").rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname = 'gradorra_test'").fetchone()
        if not exists:
            conn.execute("create database gradorra_test")


_ensure_database()

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import District  # noqa: E402
from app.seed_data import UP_DISTRICTS  # noqa: E402

engine = create_engine(TEST_URL, future=True)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    for i, name in enumerate(UP_DISTRICTS, start=1):
        db.add(District(code=f"{i:02d}", name=name, state="Uttar Pradesh"))
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    """A session inside a transaction that is rolled back after the test."""
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def verified(client):
    """Returns (mobile, headers) for a mobile that has passed OTP."""
    def _verify(mobile: str = "9876500001"):
        sent = client.post("/api/v1/otp/send", json={"mobile": mobile})
        assert sent.status_code == 200, sent.text
        code = sent.json()["dev_code"]
        v = client.post("/api/v1/otp/verify", json={"mobile": mobile, "code": code})
        assert v.status_code == 200, v.text
        return mobile, {"X-Form-Token": v.json()["form_token"]}
    return _verify


@pytest.fixture
def registration(client, verified):
    """A PENDING_PAYMENT registration, plus the headers that created it."""
    mobile, headers = verified()
    r = client.post(
        "/api/v1/registrations",
        headers=headers,
        json={
            "full_name": "Test Student", "father_name": "Test Father",
            "class_level": 10, "district_id": 49,
            "address_line": "1 Test Road, Lucknow", "email": "student@example.com",
            "consent_whatsapp": True,
        },
    )
    assert r.status_code == 201, r.text
    return r.json(), headers, mobile


@pytest.fixture
def paid(db, registration):
    """Takes a registration all the way to PAID with an acknowledgement."""
    import uuid
    from app.models import Payment, PaymentStatus
    from app.services.payments import apply_payment

    reg, headers, mobile = registration
    order_id = f"order_T{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_T{uuid.uuid4().hex[:12]}"
    db.add(Payment(
        registration_id=uuid.UUID(reg["id"]), razorpay_order_id=order_id,
        amount_paise=reg["fee_amount_paise"], status=PaymentStatus.CREATED,
    ))
    db.flush()
    result = apply_payment(db, payment_id, {
        "id": payment_id, "order_id": order_id, "status": "captured",
        "amount": reg["fee_amount_paise"], "currency": "INR", "method": "upi",
    })
    db.flush()
    return reg, headers, mobile, result
