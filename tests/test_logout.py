from app.models import RevokedToken


def test_logout_ends_the_login(client, verified):
    mobile, headers = verified("9855500001")
    r = client.post("/api/v1/logout", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"logged_out": True}


def test_a_logged_out_token_is_refused_everywhere(client, registration):
    reg, headers, mobile = registration
    assert client.get("/api/v1/me", headers=headers).status_code == 200

    client.post("/api/v1/logout", headers=headers)

    for call in (
        lambda: client.get("/api/v1/me", headers=headers),
        lambda: client.post("/api/v1/acknowledgements/lookup", headers=headers, json={"mobile": mobile}),
        lambda: client.post("/api/v1/logout", headers=headers),
    ):
        r = call()
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "FORM_TOKEN_REVOKED"


def test_logging_out_one_session_leaves_the_others(client, verified):
    _, first = verified("9855500002")
    # a second login from another device, same mobile
    _, second = verified("9855500002")
    client.post("/api/v1/logout", headers=first)
    assert client.post("/api/v1/logout", headers=second).status_code == 200


def test_logout_needs_a_token(client):
    r = client.post("/api/v1/logout")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "FORM_TOKEN_MISSING"


def test_expired_denylist_rows_are_cleaned_up(client, db, verified):
    from datetime import datetime, timedelta, timezone
    db.add(RevokedToken(jti="old", expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db.flush()
    _, headers = verified("9855500003")
    client.post("/api/v1/logout", headers=headers)
    assert db.get(RevokedToken, "old") is None


def test_a_token_without_an_id_still_works_until_it_expires(client, registration):
    """Tokens issued before logout existed carry no jti; they must not be locked out."""
    import jwt
    from datetime import datetime, timedelta, timezone
    from app.config import settings

    _, _, mobile = registration
    legacy = jwt.encode(
        {"sub": mobile, "scope": "form", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        settings.jwt_secret, algorithm="HS256",
    )
    assert client.get("/api/v1/me", headers={"X-Form-Token": legacy}).status_code == 200
