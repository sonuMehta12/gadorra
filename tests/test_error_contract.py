"""The error shape the documentation promises the front end.

If any of these fail, the API docs have become a lie. Every failure returns:

    {"detail": {"code": "...", "message": "...", "fields": {"name": "why"}}}

`fields` is present only when specific inputs are at fault.
"""
import pytest

VALID = {
    "full_name": "Anjali Singh", "father_name": "Rajesh Singh",
    "class_level": 11, "district_id": 49,
    "address_line": "45 Hazratganj, Lucknow", "email": "anjali@example.com",
    "consent_whatsapp": True,
}


def assert_shape(response, expected_code: str | None = None):
    body = response.json()
    assert "detail" in body, body
    detail = body["detail"]
    assert isinstance(detail, dict), f"detail must be an object, got {type(detail).__name__}: {detail}"
    assert isinstance(detail.get("code"), str) and detail["code"], detail
    assert isinstance(detail.get("message"), str) and detail["message"], detail
    if "fields" in detail:
        assert isinstance(detail["fields"], dict)
        assert all(isinstance(v, str) for v in detail["fields"].values())
    if expected_code:
        assert detail["code"] == expected_code, f"expected {expected_code}, got {detail['code']}"
    return detail


def test_validation_errors_name_the_fields(client):
    r = client.post("/api/v1/otp/send", json={"mobile": "123"})
    assert r.status_code == 422
    detail = assert_shape(r, "VALIDATION_ERROR")
    assert "mobile" in detail["fields"]


def test_several_bad_fields_are_all_reported(client, verified):
    _, headers = verified("9844400001")
    r = client.post("/api/v1/registrations", headers=headers,
                    json={**VALID, "full_name": "A", "class_level": 8, "address_line": "x"})
    assert r.status_code == 422
    detail = assert_shape(r, "VALIDATION_ERROR")
    assert {"full_name", "class_level", "address_line"} <= set(detail["fields"])


def test_a_missing_token_header_says_so(client):
    r = client.post("/api/v1/registrations", json=VALID)
    assert r.status_code == 422
    assert_shape(r, "FORM_TOKEN_MISSING")


def test_a_junk_token_is_reported_as_invalid(client):
    r = client.post("/api/v1/registrations", headers={"X-Form-Token": "junk"}, json=VALID)
    assert r.status_code == 401
    assert_shape(r, "FORM_TOKEN_INVALID")


@pytest.mark.parametrize("code,call", [
    ("OTP_NOT_FOUND", lambda c: c.post("/api/v1/otp/verify", json={"mobile": "9844499999", "code": "111111"})),
    ("NOT_FOUND", lambda c: c.get("/api/v1/registrations/00000000-0000-0000-0000-000000000000")),
    ("SIGNATURE_INVALID", lambda c: c.post("/api/v1/payments/verify", json={
        "razorpay_order_id": "order_x", "razorpay_payment_id": "pay_x", "razorpay_signature": "bad"})),
])
def test_documented_codes_are_real(client, code, call):
    r = call(client)
    assert r.status_code >= 400
    assert_shape(r, code)


def test_already_registered_after_paying(client, paid):
    _, headers, _, _ = paid
    r = client.post("/api/v1/registrations", headers=headers, json=VALID)
    assert r.status_code == 409
    assert_shape(r, "ALREADY_REGISTERED")


def test_looking_up_another_mobile_is_forbidden(client, paid):
    _, headers, _, _ = paid
    r = client.post("/api/v1/acknowledgements/lookup", headers=headers, json={"mobile": "9000000009"})
    assert r.status_code == 403
    assert_shape(r, "MOBILE_NOT_VERIFIED")


def test_no_receipt_before_payment(client, registration):
    reg, _, _ = registration
    r = client.get(f"/api/v1/receipts/{reg['id']}")
    assert r.status_code == 409
    assert_shape(r, "NOT_PAID")


def test_the_hourly_otp_cap_reports_its_code(client):
    mobile = "9844400002"
    from app.config import settings
    for _ in range(settings.otp_max_resends):
        client.post("/api/v1/otp/send", json={"mobile": mobile})
    r = client.post("/api/v1/otp/send", json={"mobile": mobile})
    assert r.status_code == 429
    assert_shape(r, "OTP_RATE_LIMITED")


def test_rate_limiting_uses_the_same_shape(client, monkeypatch):
    from app.config import settings
    from app.rate_limit import RateLimiter
    import app.main as main

    monkeypatch.setattr(main, "limiter", RateLimiter())
    monkeypatch.setattr(settings, "rate_limit_registration", 1)

    client.post("/api/v1/registrations", json=VALID)
    r = client.post("/api/v1/registrations", json=VALID)
    assert r.status_code == 429
    assert_shape(r, "RATE_LIMITED")
    assert "Retry-After" in r.headers


def test_every_documented_code_appears_in_the_openapi_schema(client):
    """The schema must mention the codes, or the docs send people hunting."""
    schema = client.get("/openapi.json").json()
    text = str(schema)
    for code in [
        "OTP_RATE_LIMITED", "OTP_INVALID", "OTP_EXPIRED", "FORM_TOKEN_EXPIRED",
        "ALREADY_REGISTERED", "SIGNATURE_INVALID", "AMOUNT_MISMATCH",
        "MOBILE_NOT_VERIFIED", "NOT_PAID", "RATE_LIMITED",
    ]:
        assert code in text, f"{code} is not documented anywhere in the OpenAPI schema"
