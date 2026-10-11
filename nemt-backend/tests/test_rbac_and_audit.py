from tests.helpers import PW, bearer, login, make_company, make_user


async def test_permission_matrix(client, super_token):
    a = await make_company(client, super_token, "alpha")
    await make_user(client, a, "disp@alpha.example.com", "dispatcher")
    await make_user(client, a, "rider@alpha.example.com", "passenger")
    disp = bearer((await login(client, "alpha", "disp@alpha.example.com"))["access_token"])
    rider = bearer((await login(client, "alpha", "rider@alpha.example.com"))["access_token"])

    assert (await client.get("/users", headers=rider)).status_code == 403      # passenger: no
    assert (await client.get("/users", headers=disp)).status_code == 403       # dispatcher lacks users:read
    assert (await client.get("/audit-logs", headers=disp)).status_code == 403
    assert (await client.post("/users", headers=disp, json={
        "email": "n@alpha.example.com", "password": PW, "full_name": "N", "role": "driver"})).status_code == 403
    assert (await client.get("/platform/companies", headers=a["admin_headers"])).status_code == 403
    assert (await client.get("/users", headers=a["admin_headers"])).status_code == 200


async def test_company_admin_cannot_mint_super_admin(client, super_token):
    a = await make_company(client, super_token, "alpha")
    r = await client.post("/users", headers=a["admin_headers"], json={
        "email": "x@alpha.example.com", "password": PW, "full_name": "X", "role": "super_admin"})
    assert r.status_code == 403
    me = (await client.get("/users/me", headers=a["admin_headers"])).json()
    r = await client.patch(f"/users/{me['id']}", headers=a["admin_headers"], json={"role": "super_admin"})
    assert r.status_code == 403


async def test_admin_cannot_lock_themselves_out(client, super_token):
    a = await make_company(client, super_token, "alpha")
    me = (await client.get("/users/me", headers=a["admin_headers"])).json()
    r = await client.patch(f"/users/{me['id']}", headers=a["admin_headers"], json={"is_active": False})
    assert r.status_code == 400


async def test_deactivated_user_loses_access_immediately(client, super_token):
    a = await make_company(client, super_token, "alpha")
    u = await make_user(client, a, "disp@alpha.example.com", "dispatcher")
    tokens = await login(client, "alpha", "disp@alpha.example.com")
    assert (await client.patch(f"/users/{u['id']}", headers=a["admin_headers"], json={"is_active": False})).status_code == 200
    assert (await client.get("/users/me", headers=bearer(tokens["access_token"]))).status_code == 401
    assert (await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 401
    r = await client.post("/auth/login", headers={"X-Tenant": "alpha"},
                          json={"email": "disp@alpha.example.com", "password": PW})
    assert r.status_code == 401


async def test_duplicate_email_and_slug_conflicts(client, super_token):
    a = await make_company(client, super_token, "alpha")
    await make_user(client, a, "d@alpha.example.com", "dispatcher")
    r = await client.post("/users", headers=a["admin_headers"], json={
        "email": "D@alpha.example.com", "password": PW, "full_name": "D2", "role": "driver"})
    assert r.status_code == 409                               # case-insensitive uniqueness
    dup = await client.post("/platform/companies", headers=bearer(super_token), json={
        "name": "Again", "slug": "alpha", "admin_email": "z@z.example.com",
        "admin_password": PW, "admin_full_name": "Z"})
    assert dup.status_code == 409
    reserved = await client.post("/platform/companies", headers=bearer(super_token), json={
        "name": "Sneaky", "slug": "admin", "admin_email": "z@z.example.com",
        "admin_password": PW, "admin_full_name": "Z"})
    assert reserved.status_code == 409


async def test_super_admin_lists_companies_but_not_platform_itself(client, super_token):
    await make_company(client, super_token, "alpha")
    await make_company(client, super_token, "bravo")
    r = await client.get("/platform/companies", headers=bearer(super_token))
    assert {c["slug"] for c in r.json()} == {"alpha", "bravo"}


async def test_audit_trail_is_recorded_and_tenant_scoped(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    await make_user(client, a, "d@alpha.example.com", "dispatcher")
    await client.post("/auth/login", headers={"X-Tenant": "alpha"},
                      json={"email": a["admin_email"], "password": "wrong-wrong-wrong"})

    logs_a = (await client.get("/audit-logs", headers=a["admin_headers"], params={"limit": 200})).json()
    actions_a = {row["action"] for row in logs_a}
    assert {"auth.login", "auth.login_failed", "user.create"} <= actions_a
    assert all(row["company_id"] == a["company"]["id"] for row in logs_a)

    logs_b = (await client.get("/audit-logs", headers=b["admin_headers"], params={"limit": 200})).json()
    assert "user.create" not in {row["action"] for row in logs_b}   # A's activity invisible to B
    assert all(row["company_id"] == b["company"]["id"] for row in logs_b)
    assert "metadata" in logs_a[0]                                  # alias for the frontend


async def test_security_headers_present(client):
    r = await client.get("/health")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"