from datetime import datetime, timedelta, timezone

from app.models import OtpRequest


def test_send_returns_a_dev_code_on_the_console_channel(client):
    r = client.post("/api/v1/otp/send", json={"mobile": "9811100001"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert body["channel"] == "console"
    assert len(body["dev_code"]) == 6


def test_invalid_mobile_is_rejected(client):
    assert client.post("/api/v1/otp/send", json={"mobile": "12345"}).status_code == 422
    assert client.post("/api/v1/otp/send", json={"mobile": "1234567890"}).status_code == 422


def test_verify_issues_a_form_token(client, verified):
    mobile, headers = verified("9811100002")
    assert headers["X-Form-Token"]


def test_wrong_code_is_rejected_and_counts_an_attempt(client, db):
    mobile = "9811100003"
    client.post("/api/v1/otp/send", json={"mobile": mobile})
    r = client.post("/api/v1/otp/verify", json={"mobile": mobile, "code": "000000"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "OTP_INVALID"
    otp = db.query(OtpRequest).filter(OtpRequest.mobile == mobile).order_by(OtpRequest.created_at.desc()).first()
    assert otp.attempts == 1


def test_an_expired_code_is_refused(client, db):
    mobile = "9811100004"
    code = client.post("/api/v1/otp/send", json={"mobile": mobile}).json()["dev_code"]
    otp = db.query(OtpRequest).filter(OtpRequest.mobile == mobile).one()
    otp.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.flush()
    r = client.post("/api/v1/otp/verify", json={"mobile": mobile, "code": code})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "OTP_EXPIRED"


def test_a_code_cannot_be_used_twice(client):
    mobile = "9811100005"
    code = client.post("/api/v1/otp/send", json={"mobile": mobile}).json()["dev_code"]
    assert client.post("/api/v1/otp/verify", json={"mobile": mobile, "code": code}).status_code == 200
    second = client.post("/api/v1/otp/verify", json={"mobile": mobile, "code": code})
    assert second.status_code == 400


def test_requesting_a_new_code_kills_the_previous_one(client):
    mobile = "9811100006"
    first = client.post("/api/v1/otp/send", json={"mobile": mobile}).json()["dev_code"]
    second = client.post("/api/v1/otp/send", json={"mobile": mobile}).json()["dev_code"]
    assert client.post("/api/v1/otp/verify", json={"mobile": mobile, "code": first}).status_code == 400
    assert client.post("/api/v1/otp/verify", json={"mobile": mobile, "code": second}).status_code == 200


def test_hourly_resend_cap(client):
    mobile = "9811100007"
    for _ in range(3):
        assert client.post("/api/v1/otp/send", json={"mobile": mobile}).status_code == 200
    r = client.post("/api/v1/otp/send", json={"mobile": mobile})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "OTP_RATE_LIMITED"
