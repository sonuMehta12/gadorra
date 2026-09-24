"""Login by OTP, then GET /me."""


def test_me_needs_a_verified_mobile(client):
    r = client.get("/api/v1/me")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "FORM_TOKEN_MISSING"


def test_a_mobile_that_never_registered_gets_not_registered(client, verified):
    _, headers = verified("9855500001")
    r = client.get("/api/v1/me", headers=headers)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_REGISTERED"


def test_an_unpaid_registration_shows_as_pending_with_no_receipt(client, registration):
    reg, headers, mobile = registration
    body = client.get("/api/v1/me", headers=headers).json()
    assert body["student"]["mobile"] == mobile
    assert body["student"]["father_name"] == "Test Father"
    assert body["student"]["district_name"] == "Lucknow"
    [only] = body["registrations"]
    assert only["id"] == reg["id"]
    assert only["status"] == "PENDING_PAYMENT"
    assert only["acknowledgement_number"] is None
    assert only["receipt_url"] is None


def test_a_paid_registration_carries_the_number_and_working_receipt_links(client, paid):
    reg, headers, _, result = paid
    body = client.get("/api/v1/me", headers=headers).json()
    [only] = body["registrations"]
    assert only["status"] == "PAID"
    assert only["acknowledgement_number"] == result.acknowledgement.number
    assert only["paid_at"] is not None
    assert client.get(only["receipt_url"]).status_code == 200
    pdf = client.get(only["receipt_pdf_url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_you_only_ever_see_your_own_profile(client, paid, verified):
    reg, _, _, _ = paid
    _, stranger = verified("9855500002")
    r = client.get("/api/v1/me", headers=stranger)
    assert r.status_code == 404  # the stranger has no registration of their own
    assert reg["id"] not in r.text
