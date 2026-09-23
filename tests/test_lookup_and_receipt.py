from app.config import settings


def test_lookup_by_acknowledgement_number_gives_the_discount(client, paid):
    _, headers, _, result = paid
    r = client.post("/api/v1/acknowledgements/lookup", headers=headers,
                    json={"acknowledgement_number": result.acknowledgement.number})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["payable_fee_paise"] == settings.fee_postlaunch_discounted_paise
    assert body["discount_paise"] == settings.fee_postlaunch_paise - settings.fee_postlaunch_discounted_paise
    assert body["prefill"]["father_name"] == "Test Father"


def test_lookup_by_the_verified_mobile_works(client, paid):
    _, headers, mobile, _ = paid
    r = client.post("/api/v1/acknowledgements/lookup", headers=headers, json={"mobile": mobile})
    assert r.json()["found"] is True


def test_you_cannot_look_up_someone_elses_mobile(client, paid):
    _, headers, _, _ = paid
    r = client.post("/api/v1/acknowledgements/lookup", headers=headers, json={"mobile": "9000000009"})
    assert r.status_code == 403


def test_lookup_needs_a_verified_token(client, paid):
    _, _, _, result = paid
    r = client.post("/api/v1/acknowledgements/lookup",
                    json={"acknowledgement_number": result.acknowledgement.number})
    assert r.status_code == 422


def test_an_unknown_number_charges_the_full_fee(client, paid):
    _, headers, _, _ = paid
    r = client.post("/api/v1/acknowledgements/lookup", headers=headers,
                    json={"acknowledgement_number": "GPET26/UP01/00000"})
    assert r.json()["found"] is False
    assert r.json()["payable_fee_paise"] == settings.fee_postlaunch_paise


def test_a_redeemed_number_loses_the_discount(client, db, paid):
    _, headers, _, result = paid
    result.acknowledgement.redeemed = True
    db.flush()
    r = client.post("/api/v1/acknowledgements/lookup", headers=headers,
                    json={"acknowledgement_number": result.acknowledgement.number})
    assert r.json()["redeemed"] is True
    assert r.json()["payable_fee_paise"] == settings.fee_postlaunch_paise


def test_no_receipt_before_payment(client, registration):
    reg, _, _ = registration
    r = client.get(f"/api/v1/receipts/{reg['id']}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NOT_PAID"


def test_receipt_json_carries_the_student_and_the_number(client, paid):
    reg, _, _, result = paid
    body = client.get(f"/api/v1/receipts/{reg['id']}").json()
    assert body["acknowledgement_number"] == result.acknowledgement.number
    assert body["student"]["name"] == "Test Student"
    assert body["payment"]["amount"] == "Rs 99.00"
    assert body["receipt_id"].endswith(result.acknowledgement.number.rsplit("/", 1)[-1])
    assert len(body["perks"]) == 4


def test_receipt_html_and_pdf_render(client, paid):
    reg, _, _, _ = paid
    html = client.get(f"/api/v1/receipts/{reg['id']}/view")
    assert html.status_code == 200 and "<html" in html.text.lower()
    pdf = client.get(f"/api/v1/receipts/{reg['id']}/receipt.pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    assert "attachment" in pdf.headers["content-disposition"]
