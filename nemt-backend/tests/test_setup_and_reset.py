"""First-run setup and the Super Admin system reset (runs only against the *_test database)."""

from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.db import open_tenant_session
from app.scripts.bootstrap import main as bootstrap_super_admin
from tests.helpers import (
    PW,
    at,
    bearer,
    login,
    make_company,
    new_trip,
    staff_headers,
    upload,
    world,
)

TOKEN = "setup-token-for-tests-0123456789"
RESET = {"confirm": "RESET SYSTEM", "password": PW}


async def owner_sql(statement: str):
    eng = create_async_engine(settings.database_owner_url, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            result = await conn.execute(text(statement))
            return result.all() if result.returns_rows else []
    finally:
        await eng.dispose()


@pytest.fixture
def setup_token(monkeypatch):
    monkeypatch.setattr(settings, "setup_token", SecretStr(TOKEN))


# ------------------------------------------------------------------ setup
async def test_web_setup_is_disabled_without_a_server_token(client, monkeypatch):
    monkeypatch.setattr(settings, "setup_token", None)
    st = (await client.get("/setup/status")).json()
    assert st == {"needs_setup": True, "web_setup_enabled": False, "platform_slug": "platform"}
    r = await client.post("/setup/super-admin", json={"setup_token": "x", "email": "a@b.example.com",
                                                      "password": PW, "full_name": "A"})
    assert r.status_code == 403


async def test_web_setup_creates_exactly_one_first_super_admin(client, setup_token):
    body = {"setup_token": TOKEN, "email": "Root@Example.com", "password": PW, "full_name": "Root"}
    assert (await client.post("/setup/super-admin", json=body | {"setup_token": "wrong"})).status_code == 403
    assert (await client.post("/setup/super-admin", json=body | {"password": "short"})).status_code == 422
    r = await client.post("/setup/super-admin", json=body)
    assert r.status_code == 201 and r.json()["email"] == "root@example.com"
    assert (await client.get("/setup/status")).json()["needs_setup"] is False
    again = await client.post("/setup/super-admin", json=body | {"email": "second@example.com"})
    assert again.status_code == 409                       # setup only ever works on a fresh system
    tokens = await login(client, "platform", "root@example.com")
    me = (await client.get("/users/me", headers=bearer(tokens["access_token"]))).json()
    assert me["role"] == "super_admin"


# ------------------------------------------------------------------ reset: authorization
async def test_reset_requires_super_admin_phrase_and_password(client, super_token, monkeypatch):
    c = await make_company(client, super_token, "alpha")
    disp = await staff_headers(client, c, "dispatcher")
    for h in (c["admin_headers"], disp):
        assert (await client.post("/platform/system/reset", headers=h, json=RESET)).status_code == 403
        assert (await client.get("/platform/system/reset-preview", headers=h)).status_code == 403
    assert (await client.post("/platform/system/reset", json=RESET)).status_code == 401
    root = bearer(super_token)
    bad_phrase = await client.post("/platform/system/reset", headers=root, json=RESET | {"confirm": "reset"})
    assert bad_phrase.status_code == 422
    bad_pw = await client.post("/platform/system/reset", headers=root, json=RESET | {"password": "x" * 14})
    assert bad_pw.status_code == 403
    monkeypatch.setattr(settings, "allow_system_reset", False)
    assert (await client.post("/platform/system/reset", headers=root, json=RESET)).status_code == 403
    preview = (await client.get("/platform/system/reset-preview", headers=root)).json()
    assert preview["enabled"] is False
    # nothing was touched by any refused attempt
    assert (await client.get("/users/me", headers=c["admin_headers"])).status_code == 200


# ------------------------------------------------------------------ reset: the real thing
async def test_full_reset_returns_to_fresh_install(client, super_token, setup_token):
    root = bearer(super_token)
    root_refresh = (await login(client, "platform", "root@platform.example.com"))["refresh_token"]
    c = await make_company(client, super_token, "alpha")
    admin = c["admin_headers"]
    w = await world(client, c)
    await new_trip(client, admin, w["patient"], w["facility_id"], at(days=2))
    assert (await upload(client, admin, "patient", w["patient"]["id"])).status_code == 201
    disp = await staff_headers(client, c, "dispatcher")
    admin_refresh = (await login(client, "alpha", c["admin_email"]))["refresh_token"]
    audit_before, audit_max_id = (await owner_sql("SELECT count(*), max(id) FROM audit_logs"))[0]

    preview = (await client.get("/platform/system/reset-preview", headers=root)).json()
    assert preview["enabled"] is True
    assert preview["will_delete"]["users"] >= 4 and preview["will_delete"]["trips"] == 1
    assert preview["will_delete"]["companies"] == 1 and "audit_logs" not in preview["will_delete"]

    r = await client.post("/platform/system/reset", headers=root, json=RESET)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["trips"] == 1 and r.json()["needs_setup"] is True

    # every old credential is dead: access tokens, refresh tokens, logins
    for h in (root, admin, disp):
        assert (await client.get("/users/me", headers=h)).status_code == 401
    for rt in (root_refresh, admin_refresh):
        assert (await client.post("/auth/refresh", json={"refresh_token": rt})).status_code == 401
    body = {"email": c["admin_email"], "password": PW}
    assert (await client.post("/auth/login", headers={"X-Tenant": "alpha"}, json=body)).status_code == 404
    body = {"email": "root@platform.example.com", "password": PW}
    assert (await client.post("/auth/login", headers={"X-Tenant": "platform"}, json=body)).status_code == 401

    # the database is empty of operational rows, but schema, platform row and audit log remain
    rows = dict(await owner_sql(
        "SELECT 'users', count(*) FROM users UNION ALL SELECT 'patients', count(*) FROM patients"
        " UNION ALL SELECT 'trips', count(*) FROM trips UNION ALL SELECT 'documents', count(*) FROM documents"
        " UNION ALL SELECT 'refresh_tokens', count(*) FROM refresh_tokens"
        " UNION ALL SELECT 'companies', count(*) FROM companies"
        " UNION ALL SELECT 'platform', count(*) FROM companies WHERE is_platform"))
    assert rows == {"users": 0, "patients": 0, "trips": 0, "documents": 0, "refresh_tokens": 0,
                    "companies": 1, "platform": 1}
    kept, resets = (await owner_sql(
        f"SELECT count(*) FILTER (WHERE id <= {audit_max_id}),"
        " count(*) FILTER (WHERE action = 'platform.system_reset') FROM audit_logs"))[0]
    assert kept == audit_before and resets == 1          # history survives; the reset itself is logged
    assert (await client.get("/setup/status")).json()["needs_setup"] is True

    # ...and the whole hierarchy can be rebuilt from scratch, re-using the old names
    made = await client.post("/setup/super-admin", json={"setup_token": TOKEN, "email": "new-root@example.com",
                                                          "password": PW, "full_name": "New Root"})
    assert made.status_code == 201
    new_root = (await login(client, "platform", "new-root@example.com"))["access_token"]
    c2 = await make_company(client, new_root, "alpha")             # slug 'alpha' is free again
    assert (await client.get("/users", headers=c2["admin_headers"])).json()[0]["email"] == c2["admin_email"]
    d2 = await staff_headers(client, c2, "dispatcher")
    assert (await client.get("/patients", headers=d2)).json() == []
    reg = await client.post("/auth/register", headers={"X-Tenant": "alpha"}, json={
        "email": "fresh@example.com", "password": PW, "full_name": "Fresh Rider", "date_of_birth": "1980-01-01"})
    assert reg.status_code == 201
    assert len((await client.get("/patients", headers=d2)).json()) == 1


async def test_reset_is_all_or_nothing(client, super_token):
    """If any step of the reset fails, NOTHING is deleted. We force a failure by pointing a
    foreign key from outside the public schema at `users`: TRUNCATE users must then refuse."""
    c = await make_company(client, super_token, "alpha")
    await owner_sql("CREATE SCHEMA IF NOT EXISTS reset_probe")
    try:
        await owner_sql("CREATE TABLE reset_probe.pin (company_id uuid, user_id uuid,"
                        " FOREIGN KEY (company_id, user_id) REFERENCES public.users (company_id, id))")
        await owner_sql("INSERT INTO reset_probe.pin SELECT company_id, id FROM users LIMIT 1")
        r = await client.post("/platform/system/reset", headers=bearer(super_token), json=RESET)
        assert r.status_code == 503
        assert (await client.get("/users/me", headers=c["admin_headers"])).status_code == 200
        assert (await client.get("/users/me", headers=bearer(super_token))).status_code == 200
        assert (await owner_sql("SELECT count(*) FROM companies"))[0][0] == 2
    finally:
        await owner_sql("DROP SCHEMA reset_probe CASCADE")


async def test_write_racing_a_reset_gets_409_not_500(client, super_token, monkeypatch):
    """A write that commits just before a reset TRUNCATEs its table can't read its row back.
    Simulate that window (the reset itself is covered above) and expect a clean 409."""
    from sqlalchemy.exc import InvalidRequestError
    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.helpers import patient_payload

    c = await make_company(client, super_token, "alpha")

    async def vanished(self, instance, *a, **kw):
        raise InvalidRequestError(f"Could not refresh instance '{instance!r}'")

    monkeypatch.setattr(AsyncSession, "refresh", vanished)
    r = await client.post("/patients", headers=c["admin_headers"], json=patient_payload())
    assert r.status_code == 409 and "system reset" in r.json()["detail"]


async def test_write_losing_a_deadlock_gets_503_not_500(client, super_token, monkeypatch):
    """Writers can be picked as the deadlock victim while a reset TRUNCATEs. Nothing was saved,
    so the client must hear "try again" (503 + Retry-After), not an internal error."""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    from tests.helpers import patient_payload

    c = await make_company(client, super_token, "alpha")

    class Deadlock(Exception):
        sqlstate = "40P01"

    real_flush = AsyncSession.flush

    async def flush(self, *a, **kw):
        if any(type(o).__name__ == "Patient" for o in self.new):
            raise DBAPIError("INSERT INTO patients ...", {}, Deadlock("deadlock detected"))
        return await real_flush(self, *a, **kw)

    monkeypatch.setattr(AsyncSession, "flush", flush)
    r = await client.post("/patients", headers=c["admin_headers"], json=patient_payload())
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    assert "not saved" in r.json()["detail"]


async def test_reset_function_refuses_outside_platform_context(client, super_token):
    """Defence in depth: even called directly by the app role, the DB function needs bypass context."""
    c = await make_company(client, super_token, "alpha")
    async with open_tenant_session(UUID(c["company"]["id"])) as s:
        with pytest.raises(Exception, match="platform context"):
            await s.execute(text("SELECT app_system_reset()"))
    assert (await client.get("/users/me", headers=c["admin_headers"])).status_code == 200


async def test_cli_bootstrap_is_idempotent(client):
    await bootstrap_super_admin("cli@example.com", PW, "CLI")
    await bootstrap_super_admin("cli@example.com", PW, "CLI")
    assert (await client.get("/setup/status")).json()["needs_setup"] is False
    assert (await owner_sql("SELECT count(*) FROM users WHERE role = 'super_admin'"))[0][0] == 1
