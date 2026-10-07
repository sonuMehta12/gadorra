"""POST /auth/login/send-otp and /auth/login/verify -- login for registered students only."""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import Notification, OtpRequest

SEND = "/api/v1/auth/login/send-otp"
VERIFY = "/api/v1/auth/login/verify"


def _login(client, mobile):
    sent = client.post(SEND, json={"mobile": mobile})
    assert sent.status_code == 200, sent.text
    return client.post(VERIFY, json={"mobile": mobile, "code": sent.json()["dev_code"]})


def test_an_unregistered_mobile_is_turned_away_and_no_message_goes_out(client, db):
    r = client.post(SEND, json={"mobile": "9866600001"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_REGISTERED"
    assert "register" in r.json()["detail"]["message"].lower()
    assert db.query(OtpRequest).filter(OtpRequest.mobile == "9866600001").count() == 0
    assert db.query(Notification).filter(Notification.recipient.like("%9866600001")).count() == 0


def test_a_registered_but_unpaid_student_can_log_in(client, registration):
    reg, _, mobile = registration
    r = _login(client, mobile)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True and body["form_token"]
    assert body["profile"]["is_paid"] is False
    [only] = body["profile"]["registrations"]
    assert only["id"] == reg["id"] and only["status"] == "PENDING_PAYMENT"


def test_a_paid_student_gets_their_number_in_the_login_response(client, paid):
    _, _, mobile, result = paid
    body = _login(client, mobile).json()
    assert body["profile"]["is_paid"] is True
    assert body["profile"]["acknowledgement_number"] == result.acknowledgement.number


def test_the_login_profile_is_the_same_as_me(client, paid):
    _, _, mobile, _ = paid
    body = _login(client, mobile).json()
    me = client.get("/api/v1/me", headers={"X-Form-Token": body["form_token"]})
    assert me.status_code == 200
    assert me.json() == body["profile"]


def test_the_token_lasts_sixty_minutes(client, registration):
    _, _, mobile = registration
    body = _login(client, mobile).json()
    assert settings.form_token_ttl_minutes == 60
    assert body["expires_in_seconds"] == 3600


def test_verify_turns_away_an_unregistered_mobile_without_spending_its_code(client, db):
    # A code obtained through the open form flow cannot be used to log in.
    mobile = "9866600002"
    code = client.post("/api/v1/otp/send", json={"mobile": mobile}).json()["dev_code"]
    r = client.post(VERIFY, json={"mobile": mobile, "code": code})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_REGISTERED"
    otp = db.query(OtpRequest).filter(OtpRequest.mobile == mobile).one()
    assert otp.consumed is False and otp.attempts == 0


def test_a_wrong_code_is_refused_and_counted(client, registration):
    _, _, mobile = registration
    client.post(SEND, json={"mobile": mobile})
    r = client.post(VERIFY, json={"mobile": mobile, "code": "000000"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "OTP_INVALID"


def test_an_expired_code_is_refused(client, db, registration):
    _, _, mobile = registration
    code = client.post(SEND, json={"mobile": mobile}).json()["dev_code"]
    otp = db.query(OtpRequest).filter(OtpRequest.mobile == mobile, OtpRequest.consumed.is_(False)).one()
    otp.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.flush()
    r = client.post(VERIFY, json={"mobile": mobile, "code": code})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "OTP_EXPIRED"


def test_a_code_cannot_log_in_twice(client, registration):
    _, _, mobile = registration
    code = client.post(SEND, json={"mobile": mobile}).json()["dev_code"]
    assert client.post(VERIFY, json={"mobile": mobile, "code": code}).status_code == 200
    again = client.post(VERIFY, json={"mobile": mobile, "code": code})
    assert again.status_code == 400
    assert again.json()["detail"]["code"] == "OTP_NOT_FOUND"


def test_verify_without_sending_first(client, registration):
    _, _, mobile = registration
    # the registration fixture's own code was consumed when it verified
    r = client.post(VERIFY, json={"mobile": mobile, "code": "123456"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "OTP_NOT_FOUND"


def test_too_many_wrong_codes_locks_the_code(client, registration):
    _, _, mobile = registration
    code = client.post(SEND, json={"mobile": mobile}).json()["dev_code"]
    for _ in range(settings.otp_max_attempts):
        client.post(VERIFY, json={"mobile": mobile, "code": "000000"})
    r = client.post(VERIFY, json={"mobile": mobile, "code": code})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "OTP_TOO_MANY_ATTEMPTS"


def test_an_invalid_mobile_is_a_validation_error(client):
    r = client.post(SEND, json={"mobile": "12345"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_the_hourly_otp_cap_applies_to_login_too(client, registration):
    _, _, mobile = registration
    # the fixture already used one of this hour's codes through /otp/send
    for _ in range(settings.otp_max_resends - 1):
        assert client.post(SEND, json={"mobile": mobile}).status_code == 200
    r = client.post(SEND, json={"mobile": mobile})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "OTP_RATE_LIMITED"


def test_a_login_token_can_be_logged_out(client, registration):
    _, _, mobile = registration
    headers = {"X-Form-Token": _login(client, mobile).json()["form_token"]}
    assert client.post("/api/v1/logout", headers=headers).status_code == 200
    r = client.get("/api/v1/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "FORM_TOKEN_REVOKED"


def test_the_form_flow_still_works_for_a_new_mobile(client):
    # /otp/send stays open: a new student needs it to reach the form.
    r = client.post("/api/v1/otp/send", json={"mobile": "9866600003"})
    assert r.status_code == 200
