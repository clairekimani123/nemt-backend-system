"""Password recovery for every password-based account type, and the token's failure modes."""

from unittest.mock import patch

from app.core.mail import MemoryMailer
from tests.helpers import PW, login, make_company, make_user, new_driver, sql
from tests.test_passenger_portal import register

NEW_PW = "an-entirely-new-passphrase"


async def _request_token(client, slug: str, email: str) -> str:
    MemoryMailer.outbox.clear()
    r = await client.post("/auth/password/forgot", headers={"X-Tenant": slug}, json={"email": email})
    assert r.status_code == 202, r.text
    assert len(MemoryMailer.outbox) == 1 and MemoryMailer.outbox[0]["to"] == email
    body = MemoryMailer.outbox[0]["body"]
    assert "/reset-password?token=" in body
    return body.split("token=")[1].strip()


async def _reset_and_check(client, slug: str, email: str) -> None:
    token = await _request_token(client, slug, email)
    assert (await client.post("/auth/password/reset", json={"token": token, "new_password": NEW_PW})).status_code == 204
    old = await client.post("/auth/login", headers={"X-Tenant": slug}, json={"email": email, "password": PW})
    assert old.status_code == 401
    await login(client, slug, email, NEW_PW)


async def test_every_account_type_can_reset(client, super_token):
    c = await make_company(client, super_token, "alpha")
    await make_user(client, c, "disp@alpha.example.com", "dispatcher")
    await new_driver(client, c["admin_headers"], email="drv@alpha.example.com")
    await register(client, "alpha", "rider@example.com")
    await _reset_and_check(client, "platform", "root@platform.example.com")      # super admin
    await _reset_and_check(client, "alpha", c["admin_email"])                     # company admin
    await _reset_and_check(client, "alpha", "disp@alpha.example.com")             # dispatcher
    await _reset_and_check(client, "alpha", "drv@alpha.example.com")              # driver
    await _reset_and_check(client, "alpha", "rider@example.com")                  # passenger


async def test_invalid_expired_and_cross_tenant_tokens_are_rejected(client, super_token):
    await make_company(client, super_token, "alpha")
    await register(client, "alpha", "rider@example.com")
    for junk in ("", "garbage", "not-a-uuid.secret", "00000000-0000-0000-0000-000000000000.nope"):
        r = await client.post("/auth/password/reset", json={"token": junk, "new_password": NEW_PW})
        assert r.status_code in (400, 401, 422), (junk, r.status_code)

    token = await _request_token(client, "alpha", "rider@example.com")
    await sql("UPDATE password_reset_tokens SET expires_at = now() - interval '1 minute'")
    expired = await client.post("/auth/password/reset", json={"token": token, "new_password": NEW_PW})
    assert expired.status_code == 400
    await login(client, "alpha", "rider@example.com")  # password unchanged

    token = await _request_token(client, "alpha", "rider@example.com")
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    assert (await client.post("/auth/password/reset", json={"token": tampered, "new_password": NEW_PW})).status_code == 400


async def test_new_password_rules_are_enforced(client, super_token):
    await make_company(client, super_token, "alpha")
    await register(client, "alpha", "rider@example.com")
    token = await _request_token(client, "alpha", "rider@example.com")
    weak = await client.post("/auth/password/reset", json={"token": token, "new_password": "short"})
    assert weak.status_code == 422
    # the failed attempt did not burn the token
    assert (await client.post("/auth/password/reset", json={"token": token, "new_password": NEW_PW})).status_code == 204


async def test_mail_failure_does_not_change_the_response_or_leak_the_token(client, super_token, caplog):
    await make_company(client, super_token, "alpha")
    await register(client, "alpha", "rider@example.com")
    with patch("app.workers.tasks.send_email.delay", side_effect=ConnectionError("broker down")):
        r = await client.post("/auth/password/forgot", headers={"X-Tenant": "alpha"},
                              json={"email": "rider@example.com"})
    assert r.status_code == 202 and "token" not in r.text
    assert "could not enqueue password reset email" in caplog.text
    assert "token=" not in caplog.text
    unknown = await client.post("/auth/password/forgot", headers={"X-Tenant": "alpha"}, json={"email": "nobody@example.com"})
    assert unknown.status_code == 202 and unknown.json() == r.json()  # identical either way
