"""Super Admin oversight endpoints and the account hierarchy around them."""

from tests.helpers import PW, bearer, login, make_company, staff_headers


async def test_overview_and_company_users(client, super_token):
    root = bearer(super_token)
    a = await make_company(client, super_token, "alpha")
    await make_company(client, super_token, "beta")
    await staff_headers(client, a, "dispatcher")
    ov = (await client.get("/platform/overview", headers=root)).json()
    assert ov["companies"] == 2 and ov["users_by_role"]["company_admin"] == 2
    alpha = next(p for p in ov["per_company"] if p["slug"] == "alpha")
    assert alpha["users_by_role"] == {"company_admin": 1, "dispatcher": 1}

    cid = a["company"]["id"]
    users = (await client.get(f"/platform/companies/{cid}/users", headers=root)).json()
    assert sorted(u["role"] for u in users) == ["company_admin", "dispatcher"]

    made = await client.post(f"/platform/companies/{cid}/admins", headers=root, json={
        "email": "second-admin@alpha.example.com", "password": PW, "full_name": "Second Admin"})
    assert made.status_code == 201 and made.json()["role"] == "company_admin"
    dup = await client.post(f"/platform/companies/{cid}/admins", headers=root, json={
        "email": "SECOND-admin@alpha.example.com", "password": PW, "full_name": "Again"})
    assert dup.status_code == 409
    await login(client, "alpha", "second-admin@alpha.example.com")      # the new admin works

    uid = made.json()["id"]
    no = await client.patch(f"/platform/companies/{cid}/users/{uid}", headers=root, json={"role": "super_admin"})
    assert no.status_code == 403                           # super admins are never minted inside a company
    off = await client.patch(f"/platform/companies/{cid}/users/{uid}", headers=root, json={"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False
    r = await client.post("/auth/login", headers={"X-Tenant": "alpha"},
                          json={"email": "second-admin@alpha.example.com", "password": PW})
    assert r.status_code == 401


async def test_company_deactivation_blocks_its_logins(client, super_token):
    root = bearer(super_token)
    a = await make_company(client, super_token, "alpha")
    assert (await client.patch(f"/platform/companies/{a['company']['id']}", headers=root,
                               json={"is_active": False})).status_code == 200
    r = await client.post("/auth/login", headers={"X-Tenant": "alpha"},
                          json={"email": a["admin_email"], "password": PW})
    assert r.status_code == 404


async def test_super_admin_accounts(client, super_token):
    root = bearer(super_token)
    r = await client.post("/platform/super-admins", headers=root, json={
        "email": "ops@platform.example.com", "password": PW, "full_name": "Ops"})
    assert r.status_code == 201 and r.json()["role"] == "super_admin"
    listed = (await client.get("/platform/super-admins", headers=root)).json()
    assert len(listed) == 2
    me = (await client.get("/users/me", headers=root)).json()
    assert (await client.patch(f"/platform/super-admins/{me['id']}", headers=root,
                               json={"is_active": False})).status_code == 400
    off = await client.patch(f"/platform/super-admins/{r.json()['id']}", headers=root, json={"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False


async def test_platform_endpoints_refuse_everyone_else(client, super_token):
    a = await make_company(client, super_token, "alpha")
    cid = a["company"]["id"]
    disp = await staff_headers(client, a, "dispatcher")
    for h in (a["admin_headers"], disp):
        for method, path in (("GET", "/platform/overview"), ("GET", f"/platform/companies/{cid}/users"),
                             ("GET", "/platform/super-admins"), ("POST", "/platform/super-admins"),
                             ("POST", f"/platform/companies/{cid}/admins")):
            r = await client.request(method, path, headers=h,
                                     json={"email": "x@x.example.com", "password": PW, "full_name": "X"}
                                     if method == "POST" else None)
            assert r.status_code == 403, (method, path)


async def test_stats_summary_scope(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "beta")
    await staff_headers(client, b, "dispatcher")
    disp = await staff_headers(client, a, "dispatcher")
    s = (await client.get("/stats/summary", headers=disp)).json()
    assert s["users_by_role"] == {"company_admin": 1, "dispatcher": 1}   # beta's people are not counted
    assert s["patients"] == 0 and s["trips_today_by_status"] == {}
    assert (await client.get("/stats/summary")).status_code == 401
