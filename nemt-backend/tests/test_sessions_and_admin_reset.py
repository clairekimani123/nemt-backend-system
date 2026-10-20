"""Independent sessions (one per sign-in = one per browser tab), immediate sign-out, and
administrator password help (Company Administrator in-company, Super Admin across companies)."""

from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.core.db import open_tenant_session
from app.core.mail import MemoryMailer
from app.main import app as fastapi_app
from tests.helpers import PW, bearer, login, make_company, make_user, new_driver
from tests.test_passenger_portal import passenger_headers
from tests.test_realtime import _auth, _sync

NEW_PW = "a-brand-new-passphrase-42"
TEMP_PW = "Temporary-Pass-9876"


# ============================================================ sessions
async def test_each_sign_in_is_its_own_session(client, super_token):
    """Two tabs signed in as the SAME user: signing out of one leaves the other working, and the
    signed-out tab's access token stops working at once (not when it expires)."""
    c = await make_company(client, super_token, "alpha")
    await make_user(client, c, "disp@alpha.example.com", "dispatcher")
    tab1 = await login(client, "alpha", "disp@alpha.example.com")
    tab2 = await login(client, "alpha", "disp@alpha.example.com")
    assert tab1["refresh_token"] != tab2["refresh_token"]

    assert (await client.post("/auth/logout", json={"refresh_token": tab1["refresh_token"]})).status_code == 204
    assert (await client.get("/users/me", headers=bearer(tab1["access_token"]))).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": tab1["refresh_token"]})).status_code == 401
    me2 = await client.get("/users/me", headers=bearer(tab2["access_token"]))
    assert me2.status_code == 200 and me2.json()["email"] == "disp@alpha.example.com"


async def test_different_accounts_side_by_side_keep_their_identity(client, super_token):
    """Passenger, driver, dispatcher and billing signed in at once: every token answers for its
    own user only, and signing one out affects nobody else."""
    c = await make_company(client, super_token, "alpha")
    for role in ("dispatcher", "billing_staff"):
        await make_user(client, c, f"{role}@alpha.example.com", role)
    d = await new_driver(client, c["admin_headers"])
    await passenger_headers(client, "alpha", "rider@example.com")
    sessions = {
        "passenger": await login(client, "alpha", "rider@example.com"),
        "driver": await login(client, "alpha", d["email"]),
        "dispatcher": await login(client, "alpha", "dispatcher@alpha.example.com"),
        "billing_staff": await login(client, "alpha", "billing_staff@alpha.example.com"),
    }
    for role, s in sessions.items():
        assert (await client.get("/users/me", headers=bearer(s["access_token"]))).json()["role"] == role
    await client.post("/auth/logout", json={"refresh_token": sessions["driver"]["refresh_token"]})
    for role, s in sessions.items():
        code = (await client.get("/users/me", headers=bearer(s["access_token"]))).status_code
        assert code == (401 if role == "driver" else 200), role


async def test_refresh_keeps_the_session_and_reuse_kills_it(client, super_token):
    c = await make_company(client, super_token, "alpha")
    first = await login(client, "alpha", c["admin_email"])
    second = (await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})).json()
    assert (await client.get("/users/me", headers=bearer(second["access_token"]))).status_code == 200
    # the old refresh token comes back (stolen copy / duplicated tab): the whole session ends
    assert (await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})).status_code == 401
    assert (await client.get("/users/me", headers=bearer(second["access_token"]))).status_code == 401


async def test_tokens_without_session_or_audience_are_refused(client, super_token):
    c = await make_company(client, super_token, "alpha")
    real = jwt.decode(c["admin_token"], options={"verify_signature": False})
    now = datetime.now(UTC)
    base = {"sub": real["sub"], "cid": real["cid"], "typ": "access", "iss": settings.jwt_issuer,
            "iat": now, "exp": now + timedelta(minutes=5)}
    secret = settings.jwt_secret.get_secret_value()
    no_aud = jwt.encode(base | {"sid": real["sid"]}, secret, algorithm="HS256")
    wrong_aud = jwt.encode(base | {"sid": real["sid"], "aud": "someone-else"}, secret, algorithm="HS256")
    no_sid = jwt.encode(base | {"aud": settings.jwt_audience}, secret, algorithm="HS256")
    for token in (no_aud, wrong_aud, no_sid):
        assert (await client.get("/users/me", headers=bearer(token))).status_code == 401


async def test_login_errors(client, super_token):
    c = await make_company(client, super_token, "alpha")
    u = await make_user(client, c, "disp@alpha.example.com", "dispatcher")
    bad_pw = await client.post("/auth/login", headers={"X-Tenant": "alpha"}, json={"email": u["email"], "password": "nope-nope-nope"})
    unknown = await client.post("/auth/login", headers={"X-Tenant": "alpha"}, json={"email": "ghost@alpha.example.com", "password": PW})
    assert bad_pw.status_code == unknown.status_code == 401 and bad_pw.json() == unknown.json()  # no enumeration
    assert (await client.post("/auth/login", headers={"X-Tenant": "no-such-co"}, json={"email": u["email"], "password": PW})).status_code == 404
    await client.patch(f"/users/{u['id']}", headers=c["admin_headers"], json={"is_active": False})
    wrong = await client.post("/auth/login", headers={"X-Tenant": "alpha"}, json={"email": u["email"], "password": "nope-nope-nope"})
    right = await client.post("/auth/login", headers={"X-Tenant": "alpha"}, json={"email": u["email"], "password": PW})
    assert wrong.status_code == 401                                             # a guesser learns nothing
    assert right.status_code == 403 and "deactivated" in right.json()["detail"]  # the owner learns why


async def test_signing_out_closes_that_sessions_socket_only(client, super_token):
    c = await make_company(client, super_token, "alpha")
    await make_user(client, c, "disp@alpha.example.com", "dispatcher")
    tab1 = await login(client, "alpha", "disp@alpha.example.com")
    tab2 = await login(client, "alpha", "disp@alpha.example.com")

    def _inner():
        with TestClient(fastapi_app) as tc:
            with tc.websocket_connect("/ws") as ws1, tc.websocket_connect("/ws") as ws2:
                _auth(ws1, tab1["access_token"])
                _auth(ws2, tab2["access_token"])  # same user, second tab: both stay connected
                assert tc.post("/auth/logout", json={"refresh_token": tab1["refresh_token"]}).status_code == 204
                try:
                    ws1.receive_json()
                    closed = False
                except Exception:  # WebSocketDisconnect: the server closed tab 1's socket
                    closed = True
                assert closed
                ws2.send_json({"type": "ping"})
                assert ws2.receive_json()["type"] == "pong"
            # a signed-out session can't open a new socket either
            with tc.websocket_connect("/ws") as ws3:
                ws3.send_json({"type": "auth", "token": tab1["access_token"]})
                assert ws3.receive_json()["type"] == "auth_err"
    await _sync(_inner)


# ============================================================ administrator password help
async def _company_with_staff(client, super_token, slug="alpha") -> dict:
    c = await make_company(client, super_token, slug)
    u = await make_user(client, c, f"disp@{slug}.example.com", "dispatcher")
    return {"c": c, "user": u, "tokens": await login(client, slug, u["email"])}


async def test_company_admin_emails_a_reset_link(client, super_token):
    w = await _company_with_staff(client, super_token)
    MemoryMailer.outbox.clear()
    r = await client.post(f"/users/{w['user']['id']}/password-reset", headers=w["c"]["admin_headers"],
                          json={"mode": "email_link", "admin_password": PW})
    assert r.status_code == 200 and r.json()["email_queued"] is True and r.json()["sessions_revoked"] is True
    assert (await client.get("/users/me", headers=bearer(w["tokens"]["access_token"]))).status_code == 401  # signed out
    first = MemoryMailer.outbox[-1]["body"].split("token=")[1].split()[0]
    # a second request voids the first link
    await client.post(f"/users/{w['user']['id']}/password-reset", headers=w["c"]["admin_headers"],
                      json={"mode": "email_link", "admin_password": PW})
    second = MemoryMailer.outbox[-1]["body"].split("token=")[1].split()[0]
    assert (await client.post("/auth/password/reset", json={"token": first, "new_password": NEW_PW})).status_code == 400
    assert (await client.post("/auth/password/reset", json={"token": second, "new_password": NEW_PW})).status_code == 204
    assert (await client.post("/auth/password/reset", json={"token": second, "new_password": NEW_PW})).status_code == 400  # single use
    await login(client, "alpha", w["user"]["email"], NEW_PW)


async def test_temporary_password_forces_a_change(client, super_token):
    w = await _company_with_staff(client, super_token)
    r = await client.post(f"/users/{w['user']['id']}/password-reset", headers=w["c"]["admin_headers"],
                          json={"mode": "temporary_password", "temporary_password": TEMP_PW, "admin_password": PW})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert (await client.post("/auth/login", headers={"X-Tenant": "alpha"},
                              json={"email": w["user"]["email"], "password": PW})).status_code == 401  # old one gone
    h = bearer((await login(client, "alpha", w["user"]["email"], TEMP_PW))["access_token"])
    me = (await client.get("/users/me", headers=h)).json()
    assert me["must_change_password"] is True
    blocked = await client.get("/trips", headers=h)
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "password_change_required"
    assert (await client.post("/users/me/password", headers=h,
                              json={"current_password": "wrong-wrong-wrong", "new_password": NEW_PW})).status_code == 400
    assert (await client.post("/users/me/password", headers=h,
                              json={"current_password": TEMP_PW, "new_password": NEW_PW})).status_code == 204
    assert (await client.get("/trips", headers=h)).status_code == 200  # same session keeps working
    assert (await client.get("/users/me", headers=h)).json()["must_change_password"] is False

    # audited, without any password in it; stored only as an argon2 hash
    async with open_tenant_session(None, bypass=True) as s:
        rows = (await s.execute(text(
            "SELECT metadata::text FROM audit_logs WHERE action = 'user.password_reset_by_admin'"))).scalars().all()
        stored = await s.scalar(text("SELECT password_hash FROM users WHERE id = :i"), {"i": w["user"]["id"]})
    assert len(rows) == 1 and "temporary_password" in rows[0] and TEMP_PW not in rows[0]
    assert stored.startswith("$argon2") and NEW_PW not in stored


async def test_reset_needs_the_admins_own_password(client, super_token):
    w = await _company_with_staff(client, super_token)
    r = await client.post(f"/users/{w['user']['id']}/password-reset", headers=w["c"]["admin_headers"],
                          json={"mode": "temporary_password", "temporary_password": TEMP_PW, "admin_password": "not-my-password"})
    assert r.status_code == 403
    assert (await client.get("/users/me", headers=bearer(w["tokens"]["access_token"]))).status_code == 200  # untouched
    await login(client, "alpha", w["user"]["email"], PW)


async def test_company_admin_limits(client, super_token):
    a = await _company_with_staff(client, super_token, "alpha")
    b = await _company_with_staff(client, super_token, "bravo")
    admin = a["c"]["admin_headers"]
    body = {"mode": "temporary_password", "temporary_password": TEMP_PW, "admin_password": PW}
    # another company's user: indistinguishable from a missing one
    assert (await client.post(f"/users/{b['user']['id']}/password-reset", headers=admin, json=body)).status_code == 404
    assert (await client.post(f"/users/{b['user']['id']}/sessions/revoke", headers=admin, json={"admin_password": PW})).status_code == 404
    # another administrator: Super Admin only
    other_admin = await make_user(client, a["c"], "admin2@alpha.example.com", "company_admin")
    assert (await client.post(f"/users/{other_admin['id']}/password-reset", headers=admin, json=body)).status_code == 403
    # yourself: use My account
    me = (await client.get("/users/me", headers=admin)).json()
    assert (await client.post(f"/users/{me['id']}/password-reset", headers=admin, json=body)).status_code == 400
    # nothing changed for bravo's user
    await login(client, "bravo", b["user"]["email"], PW)


async def test_non_admins_cannot_use_password_tools(client, super_token):
    w = await _company_with_staff(client, super_token)
    c = w["c"]
    d = await new_driver(client, c["admin_headers"])
    rider = await passenger_headers(client, "alpha", "rider@example.com")
    await make_user(client, c, "billing@alpha.example.com", "billing_staff")
    callers = {
        "dispatcher": bearer(w["tokens"]["access_token"]),
        "driver": bearer((await login(client, "alpha", d["email"]))["access_token"]),
        "passenger": rider,
        "billing": bearer((await login(client, "alpha", "billing@alpha.example.com"))["access_token"]),
    }
    target = w["user"]["id"]
    body = {"mode": "email_link", "admin_password": PW}
    for who, h in callers.items():
        assert (await client.post(f"/users/{target}/password-reset", headers=h, json=body)).status_code == 403, who
        assert (await client.post(f"/users/{target}/sessions/revoke", headers=h, json={"admin_password": PW})).status_code == 403, who
        assert (await client.get("/platform/users", headers=h)).status_code == 403, who
        assert (await client.post(f"/platform/companies/{c['company']['id']}/users/{target}/password-reset",
                                  headers=h, json=body)).status_code == 403, who


async def test_super_admin_finds_and_helps_anyone(client, super_token):
    a = await _company_with_staff(client, super_token, "alpha")
    b = await _company_with_staff(client, super_token, "bravo")
    root = bearer(super_token)
    found = (await client.get("/platform/users", headers=root, params={"q": "disp@"})).json()
    assert {(u["email"], u["company_slug"]) for u in found} == {("disp@alpha.example.com", "alpha"), ("disp@bravo.example.com", "bravo")}
    # helps a Company Administrator who lost access (a company admin could not do this)
    admin = (await client.get("/platform/users", headers=root, params={"q": "admin@bravo"})).json()[0]
    r = await client.post(f"/platform/companies/{admin['company_id']}/users/{admin['id']}/password-reset", headers=root,
                          json={"mode": "temporary_password", "temporary_password": TEMP_PW, "admin_password": PW})
    assert r.status_code == 200
    assert (await client.get("/users/me", headers=b["c"]["admin_headers"])).status_code == 401  # their sessions ended
    await login(client, "bravo", admin["email"], TEMP_PW)
    # sign someone out everywhere
    assert (await client.post(f"/platform/companies/{a['c']['company']['id']}/users/{a['user']['id']}/sessions/revoke",
                              headers=root, json={"admin_password": PW})).status_code == 204
    assert (await client.get("/users/me", headers=bearer(a["tokens"]["access_token"]))).status_code == 401
    # wrong company in the path = not found (no cross-wiring)
    assert (await client.post(f"/platform/companies/{b['c']['company']['id']}/users/{a['user']['id']}/password-reset",
                              headers=root, json={"mode": "email_link", "admin_password": PW})).status_code == 404
    async with open_tenant_session(None, bypass=True) as s:
        actions = set((await s.execute(text("SELECT action FROM audit_logs"))).scalars().all())
    assert {"user.password_reset_by_admin", "platform.user_sessions_revoked"} <= actions


async def test_own_password_change_signs_out_other_sessions(client, super_token):
    c = await make_company(client, super_token, "alpha")
    await make_user(client, c, "disp@alpha.example.com", "dispatcher")
    here = await login(client, "alpha", "disp@alpha.example.com")
    elsewhere = await login(client, "alpha", "disp@alpha.example.com")
    r = await client.post("/users/me/password", headers=bearer(here["access_token"]),
                          json={"current_password": PW, "new_password": NEW_PW})
    assert r.status_code == 204
    assert (await client.get("/users/me", headers=bearer(here["access_token"]))).status_code == 200
    assert (await client.get("/users/me", headers=bearer(elsewhere["access_token"]))).status_code == 401
