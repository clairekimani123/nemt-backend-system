"""The most important tests in the project: tenant A must never touch tenant B."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, open_tenant_session
from app.modules.auth.models import RefreshToken
from tests.helpers import PW, bearer, make_company, make_user


async def _two_tenants(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    return a, b


# ---------------- API level ----------------
async def test_user_lists_are_isolated(client, super_token):
    a, b = await _two_tenants(client, super_token)
    await make_user(client, a, "dispatch@alpha.example.com", "dispatcher")
    emails_a = {u["email"] for u in (await client.get("/users", headers=a["admin_headers"])).json()}
    emails_b = {u["email"] for u in (await client.get("/users", headers=b["admin_headers"])).json()}
    assert emails_a == {"admin@alpha.example.com", "dispatch@alpha.example.com"}
    assert emails_b == {"admin@bravo.example.com"}


async def test_cannot_modify_other_tenants_user(client, super_token):
    a, b = await _two_tenants(client, super_token)
    victim = await make_user(client, a, "dispatch@alpha.example.com", "dispatcher")
    r = await client.patch(f"/users/{victim['id']}", headers=b["admin_headers"], json={"is_active": False})
    assert r.status_code == 404  # existence not revealed


async def test_same_email_can_exist_in_two_tenants(client, super_token):
    await _two_tenants(client, super_token)
    for slug, pw in (("alpha", "alpha-password-1"), ("bravo", "bravo-password-2")):
        r = await client.post("/auth/register", headers={"X-Tenant": slug},
                              json={"email": "sam@example.com", "password": pw, "full_name": "Sam"})
        assert r.status_code == 201
    # credentials are tenant-specific
    r = await client.post("/auth/login", headers={"X-Tenant": "bravo"},
                          json={"email": "sam@example.com", "password": "alpha-password-1"})
    assert r.status_code == 401


async def test_suspended_company_cannot_log_in(client, super_token):
    a = await make_company(client, super_token, "alpha")
    r = await client.patch(f"/platform/companies/{a['company']['id']}", headers=bearer(super_token),
                           json={"is_active": False})
    assert r.status_code == 200
    r = await client.post("/auth/login", headers={"X-Tenant": "alpha"},
                          json={"email": a["admin_email"], "password": PW})
    assert r.status_code == 404


# ---------------- database level (the safety net) ----------------
async def test_db_role_cannot_bypass_rls():
    async with SessionLocal() as s:
        row = (await s.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
    assert tuple(row) == (False, False)


async def test_rls_fails_closed_without_tenant(client, super_token):
    await _two_tenants(client, super_token)
    async with SessionLocal() as s:  # no tenant context at all
        assert await s.scalar(text("SELECT count(*) FROM users")) == 0
        assert await s.scalar(text("SELECT count(*) FROM companies")) == 0


async def test_rls_scopes_rows_to_tenant(client, super_token):
    import uuid
    a, _ = await _two_tenants(client, super_token)
    async with open_tenant_session(uuid.UUID(a["company"]["id"])) as s:
        emails = set((await s.scalars(text("SELECT email FROM users"))).all())
        assert emails == {"admin@alpha.example.com"}
        # even an UNFILTERED update only reaches our own rows
        res = await s.execute(text("UPDATE users SET full_name = 'pwned'"))
        assert res.rowcount == 1
        await s.rollback()


async def test_rls_blocks_writing_into_another_tenant(client, super_token):
    import uuid
    a, b = await _two_tenants(client, super_token)
    async with open_tenant_session(uuid.UUID(a["company"]["id"])) as s:
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.execute(text(
                "INSERT INTO users (id, company_id, email, password_hash, full_name, role)"
                " VALUES (gen_random_uuid(), :b, 'x@x.example.com', 'h', 'X', 'passenger')"
            ), {"b": uuid.UUID(b["company"]["id"])})


async def test_composite_fk_prevents_cross_tenant_links(client, super_token):
    import uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.exc import IntegrityError
    a, b = await _two_tenants(client, super_token)
    async with open_tenant_session(None, bypass=True) as s:
        user_b = await s.scalar(
            text("SELECT id FROM users WHERE company_id = :c"), {"c": uuid.UUID(b["company"]["id"])})
        s.add(RefreshToken(company_id=uuid.UUID(a["company"]["id"]), user_id=user_b,
                           token_hash="h" * 64, expires_at=datetime.now(UTC) + timedelta(days=1)))
        with pytest.raises(IntegrityError):  # token says tenant A, user belongs to tenant B
            await s.flush()


async def test_audit_log_is_append_only(client, super_token):
    await _two_tenants(client, super_token)
    async with open_tenant_session(None, bypass=True) as s:
        assert await s.scalar(text("SELECT count(*) FROM audit_logs")) > 0
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("UPDATE audit_logs SET action = 'tampered'"))
    async with open_tenant_session(None, bypass=True) as s:
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("DELETE FROM audit_logs"))


async def test_tenant_context_survives_commit(client, super_token):
    """After commit() a new transaction begins; the tenant must be re-applied (not dropped)."""
    import uuid
    a, _ = await _two_tenants(client, super_token)
    async with open_tenant_session(uuid.UUID(a["company"]["id"])) as s:
        await s.execute(text("SELECT 1"))
        await s.commit()
        emails = set((await s.scalars(text("SELECT email FROM users"))).all())
        assert emails == {"admin@alpha.example.com"}