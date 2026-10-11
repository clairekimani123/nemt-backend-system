PW = "correct-horse-battery"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def login(client, slug: str, email: str, password: str = PW) -> dict:
    r = await client.post("/auth/login", json={"email": email, "password": password},
                          headers={"X-Tenant": slug})
    assert r.status_code == 200, r.text
    return r.json()


async def make_company(client, super_token: str, slug: str) -> dict:
    """Create a company; returns its admin credentials + a logged-in admin token."""
    email = f"admin@{slug}.example.com"
    r = await client.post(
        "/platform/companies", headers=bearer(super_token),
        json={"name": slug.title(), "slug": slug, "admin_email": email,
              "admin_password": PW, "admin_full_name": f"{slug} admin"},
    )
    assert r.status_code == 201, r.text
    tokens = await login(client, slug, email)
    return {"slug": slug, "company": r.json(), "admin_email": email,
            "admin_token": tokens["access_token"], "admin_headers": bearer(tokens["access_token"])}


async def make_user(client, company: dict, email: str, role: str) -> dict:
    r = await client.post("/users", headers=company["admin_headers"],
                          json={"email": email, "password": PW, "full_name": email, "role": role})
    assert r.status_code == 201, r.text
    return r.json()