"""Every portal's API refuses every other role (the UI hiding a page is never the protection)."""

from tests.helpers import bearer, login, make_company, make_user, new_driver
from tests.test_passenger_portal import passenger_headers

# endpoint -> roles that may call it (everyone else must get 403)
MATRIX = {
    "/passenger/profile": {"passenger"},
    "/driver/me": {"driver"},
    "/driver/trips": {"driver"},
    "/dispatch/live": {"dispatcher", "company_admin", "super_admin"},
    "/users": {"company_admin", "super_admin"},
    "/platform/users": {"super_admin"},
    "/billing/claims": {"billing_staff", "company_admin", "super_admin"},
}


async def test_role_matrix(client, super_token):
    c = await make_company(client, super_token, "alpha")
    for role in ("dispatcher", "billing_staff"):
        await make_user(client, c, f"{role}@alpha.example.com", role)
    d = await new_driver(client, c["admin_headers"])
    headers = {
        "passenger": await passenger_headers(client, "alpha", "rider@example.com"),
        "driver": bearer((await login(client, "alpha", d["email"]))["access_token"]),
        "dispatcher": bearer((await login(client, "alpha", "dispatcher@alpha.example.com"))["access_token"]),
        "billing_staff": bearer((await login(client, "alpha", "billing_staff@alpha.example.com"))["access_token"]),
        "company_admin": c["admin_headers"],
        "super_admin": bearer(super_token),
    }
    problems = []
    for path, allowed in MATRIX.items():
        for role, h in headers.items():
            code = (await client.get(path, headers=h)).status_code
            if role == "super_admin" and path.startswith(("/passenger", "/driver")):
                # The Super Admin holds every permission (app/core/permissions.py) but has no
                # passenger/driver profile, so these find nothing: 404, never someone else's data.
                ok = code == 404
            else:
                ok = (role in allowed) == (code != 403)
            if not ok:
                problems.append(f"{role} GET {path} -> {code}")
        assert (await client.get(path)).status_code == 401, path  # and nobody without a session
    assert problems == []
