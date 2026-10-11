import pyotp

from app.core.mail import MemoryMailer
from tests.helpers import PW, bearer, login, make_company

H = lambda slug: {"X-Tenant": slug}  # noqa: E731


async def _passenger(client, slug="alpha", email="sam@example.com"):
    r = await client.post("/auth/register", headers=H(slug),
                          json={"email": email, "password": PW, "full_name": "Sam Rider"})
    assert r.status_code == 201, r.text
    return r.json()


async def test_register_login_me(client, super_token):
    await make_company(client, super_token, "alpha")
    user = await _passenger(client)
    assert user["role"] == "passenger"           # role can't be chosen by the client
    assert "password_hash" not in user and "mfa_secret_enc" not in user
    tokens = await login(client, "alpha", "sam@example.com")
    me = await client.get("/users/me", headers=bearer(tokens["access_token"]))
    assert me.status_code == 200 and me.json()["email"] == "sam@example.com"


async def test_register_ignores_role_injection(client, super_token):
    await make_company(client, super_token, "alpha")
    r = await client.post("/auth/register", headers=H("alpha"), json={
        "email": "evil@example.com", "password": PW, "full_name": "E", "role": "company_admin"})
    assert r.status_code == 201 and r.json()["role"] == "passenger"


async def test_weak_password_rejected(client, super_token):
    await make_company(client, super_token, "alpha")
    r = await client.post("/auth/register", headers=H("alpha"),
                          json={"email": "a@example.com", "password": "short", "full_name": "A"})
    assert r.status_code == 422


async def test_unknown_or_missing_tenant(client, super_token):
    body = {"email": "a@example.com", "password": PW}
    assert (await client.post("/auth/login", json=body, headers=H("nope"))).status_code == 404
    assert (await client.post("/auth/login", json=body)).status_code == 422


async def test_wrong_password_and_unknown_user_look_identical(client, super_token):
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    r1 = await client.post("/auth/login", headers=H("alpha"), json={"email": "sam@example.com", "password": "x" * 14})
    r2 = await client.post("/auth/login", headers=H("alpha"), json={"email": "ghost@example.com", "password": "x" * 14})
    assert r1.status_code == r2.status_code == 401 and r1.json() == r2.json()


async def test_lockout_after_repeated_failures(client, super_token):
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    for _ in range(5):
        r = await client.post("/auth/login", headers=H("alpha"), json={"email": "sam@example.com", "password": "wrong-wrong-wrong"})
        assert r.status_code == 401
    # now even the CORRECT password is refused
    r = await client.post("/auth/login", headers=H("alpha"), json={"email": "sam@example.com", "password": PW})
    assert r.status_code == 423


async def test_refresh_rotation_and_reuse_detection(client, super_token):
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    t1 = await login(client, "alpha", "sam@example.com")
    r = await client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"]
    # replaying the OLD token = theft signal -> whole family revoked, including the new token
    assert (await client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]})).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]})).status_code == 401


async def test_logout_revokes_refresh_token(client, super_token):
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    t = await login(client, "alpha", "sam@example.com")
    assert (await client.post("/auth/logout", json={"refresh_token": t["refresh_token"]})).status_code == 204
    assert (await client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]})).status_code == 401


async def test_garbage_and_wrong_type_tokens_rejected(client, super_token):
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    t = await login(client, "alpha", "sam@example.com")
    assert (await client.get("/users/me")).status_code == 401
    assert (await client.get("/users/me", headers=bearer("garbage"))).status_code == 401
    # a refresh token (or any non-access token) must not work as a bearer
    assert (await client.get("/users/me", headers=bearer(t["refresh_token"]))).status_code == 401


async def test_password_reset_flow(client, super_token):
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    old = await login(client, "alpha", "sam@example.com")

    r = await client.post("/auth/password/forgot", headers=H("alpha"), json={"email": "sam@example.com"})
    assert r.status_code == 202
    assert len(MemoryMailer.outbox) == 1
    token = MemoryMailer.outbox[0]["body"].split("token=")[1].strip()

    new_pw = "a-brand-new-password"
    assert (await client.post("/auth/password/reset", json={"token": token, "new_password": new_pw})).status_code == 204
    # single-use
    assert (await client.post("/auth/password/reset", json={"token": token, "new_password": new_pw})).status_code == 400
    # old password dead, new one works, old sessions signed out
    bad = await client.post("/auth/login", headers=H("alpha"), json={"email": "sam@example.com", "password": PW})
    assert bad.status_code == 401
    await login(client, "alpha", "sam@example.com", new_pw)
    assert (await client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]})).status_code == 401


async def test_forgot_password_does_not_reveal_accounts(client, super_token):
    await make_company(client, super_token, "alpha")
    r = await client.post("/auth/password/forgot", headers=H("alpha"), json={"email": "ghost@example.com"})
    assert r.status_code == 202 and MemoryMailer.outbox == []


async def test_mfa_enrolment_and_login(client, super_token):
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    t = await login(client, "alpha", "sam@example.com")
    h = bearer(t["access_token"])

    setup = (await client.post("/auth/mfa/setup", headers=h)).json()
    secret = setup["secret"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    assert (await client.post("/auth/mfa/enable", headers=h, json={"code": "000000"})).status_code == 400
    assert (await client.post("/auth/mfa/enable", headers=h, json={"code": pyotp.TOTP(secret).now()})).status_code == 204

    # password alone no longer yields tokens
    step1 = await client.post("/auth/login", headers=H("alpha"), json={"email": "sam@example.com", "password": PW})
    body = step1.json()
    assert body["mfa_required"] is True and body["access_token"] is None
    # the mfa_token is not an access token
    assert (await client.get("/users/me", headers=bearer(body["mfa_token"]))).status_code == 401

    bad = await client.post("/auth/mfa/login", headers=H("alpha"), json={"mfa_token": body["mfa_token"], "code": "000000"})
    assert bad.status_code == 401
    ok = await client.post("/auth/mfa/login", headers=H("alpha"),
                           json={"mfa_token": body["mfa_token"], "code": pyotp.TOTP(secret).now()})
    assert ok.status_code == 200 and ok.json()["access_token"]


async def test_mfa_secret_is_encrypted_at_rest(client, super_token):
    from sqlalchemy import text

    from app.core.db import open_tenant_session
    await make_company(client, super_token, "alpha")
    await _passenger(client)
    t = await login(client, "alpha", "sam@example.com")
    secret = (await client.post("/auth/mfa/setup", headers=bearer(t["access_token"]))).json()["secret"]
    async with open_tenant_session(None, bypass=True) as s:
        stored = await s.scalar(text("SELECT mfa_secret_enc FROM users WHERE email = 'sam@example.com'"))
    assert stored and secret not in stored