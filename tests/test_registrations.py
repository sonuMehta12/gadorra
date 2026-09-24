import pytest


BASE = {
    "full_name": "Anjali Singh", "father_name": "Rajesh Singh",
    "class_level": 11, "district_id": 49,
    "address_line": "45 Hazratganj, Lucknow", "email": "anjali@example.com",
    "consent_whatsapp": True,
}


def test_a_verified_mobile_is_required(client):
    assert client.post("/api/v1/registrations", json=BASE).status_code == 422


def test_creates_a_student_and_a_registration(client, verified):
    _, headers = verified("9822200001")
    r = client.post("/api/v1/registrations", headers=headers, json=BASE)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "PENDING_PAYMENT"
    assert body["student"]["father_name"] == "Rajesh Singh"
    assert body["student"]["mobile"] == "9822200001"


def test_the_fee_comes_from_the_server_not_the_client(client, verified):
    _, headers = verified("9822200002")
    r = client.post("/api/v1/registrations", headers=headers, json={**BASE, "fee_amount_paise": 1})
    assert r.status_code == 201
    assert r.json()["fee_amount_paise"] == 9900


def test_the_mobile_comes_from_the_token_not_the_body(client, verified):
    _, headers = verified("9822200003")
    r = client.post("/api/v1/registrations", headers=headers, json={**BASE, "mobile": "9999999999"})
    assert r.json()["student"]["mobile"] == "9822200003"


@pytest.mark.parametrize("field,value", [
    ("full_name", "A"),
    ("father_name", ""),
    ("class_level", 8),
    ("address_line", "x"),
    ("consent_whatsapp", False),
])
def test_validation_rejects_bad_input(client, verified, field, value):
    _, headers = verified(f"98222{abs(hash(field)) % 100000:05d}")
    r = client.post("/api/v1/registrations", headers=headers, json={**BASE, field: value})
    assert r.status_code == 422


def test_an_unknown_district_is_rejected(client, verified):
    _, headers = verified("9822200004")
    r = client.post("/api/v1/registrations", headers=headers, json={**BASE, "district_id": 999})
    assert r.status_code == 400


def test_a_duplicate_submit_returns_the_same_registration(client, verified):
    _, headers = verified("9822200005")
    first = client.post("/api/v1/registrations", headers=headers, json=BASE).json()
    second = client.post("/api/v1/registrations", headers=headers, json=BASE).json()
    assert first["id"] == second["id"]


def test_registering_again_after_paying_is_refused(client, paid):
    reg, headers, _, _ = paid
    r = client.post("/api/v1/registrations", headers=headers, json=BASE)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ALREADY_REGISTERED"


def test_fetching_a_registration(client, registration):
    reg, _, _ = registration
    r = client.get(f"/api/v1/registrations/{reg['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == reg["id"]


def test_email_is_optional(client, verified):
    _, headers = verified("9822200099")
    body = {k: v for k, v in BASE.items() if k != "email"}
    r = client.post("/api/v1/registrations", headers=headers, json=body)
    assert r.status_code == 201
    assert r.json()["student"]["email"] is None


def test_a_malformed_email_is_still_rejected(client, verified):
    _, headers = verified("9822200098")
    r = client.post("/api/v1/registrations", headers=headers, json={**BASE, "email": "not-an-email"})
    assert r.status_code == 422
