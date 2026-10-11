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


async def staff_headers(client, company: dict, role: str) -> dict[str, str]:
    """Create a user with `role` in `company`, log in, return auth headers."""
    email = f"{role}@{company['slug']}.example.com"
    await make_user(client, company, email, role)
    return bearer((await login(client, company["slug"], email))["access_token"])


def patient_payload(**kw) -> dict:
    base = {"first_name": "Ada", "last_name": "Lovelace", "date_of_birth": "1950-05-17",
            "medicaid_id": "AB-123456", "notes": "Oxygen concentrator; call on arrival",
            "mobility": "wheelchair_manual", "phone": "555-0100"}
    return base | kw


def driver_payload(email: str = "dan@fleet.example.com", **kw) -> dict:
    base = {"email": email, "password": PW, "full_name": "Dan Driver", "phone": "555-0200",
            "employee_number": "E-100", "license_number": "D1234567", "license_state": "va",
            "license_expires_on": "2030-01-01", "wheelchair_certified": True}
    return base | kw


def vehicle_payload(**kw) -> dict:
    base = {"unit_number": "V-01", "vin": "1HGCM82633A004352", "plate": "ABC1234", "plate_state": "VA",
            "make": "Ford", "model": "Transit", "year": 2022, "vehicle_type": "wheelchair_van",
            "seat_capacity": 6, "wheelchair_capacity": 2, "equipment": "lift"}
    return base | kw


def facility_payload(**kw) -> dict:
    base = {"name": "Riverside Dialysis", "kind": "dialysis_center",
            "address": {"line1": "100 Main St", "city": "Arlington", "state": "va", "postal_code": "22201"}}
    return base | kw


PDF = b"%PDF-1.4\n%SECRET-MARKER-12345\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


async def upload(client, headers, owner_type: str, owner_id: str, doc_type: str = "consent",
                 data: bytes = PDF, filename: str = "doc.pdf", ctype: str = "application/pdf", **form):
    return await client.post("/documents", headers=headers,
                             data={"owner_type": owner_type, "owner_id": owner_id, "doc_type": doc_type, **form},
                             files={"file": (filename, data, ctype)})