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
    base = {"first_name": "Ada", "last_name": "Lovelace", "email": "patient@example.com", "date_of_birth": "1950-05-17",
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


# ============================================================ Phase 3 helpers (trips)
from datetime import UTC, date, datetime, time, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

NY = ZoneInfo("America/New_York")  # the default company timezone
HOME_ADDRESS = {"line1": "1 Home St", "city": "Arlington", "state": "VA", "postal_code": "22201"}


def at(days: int = 3, hour: int = 14, minute: int = 0) -> datetime:
    """A UTC instant `days` from now at hour:minute UTC (seconds zeroed so windows are exact)."""
    return (datetime.now(UTC) + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def at_local(days: int, hour: int, minute: int = 0) -> datetime:
    """A UTC instant for a New-York wall-clock time `days` from today."""
    return datetime.combine(datetime.now(NY).date() + timedelta(days=days), time(hour, minute), tzinfo=NY).astimezone(UTC)


def local_date(dt: datetime) -> date:
    return dt.astimezone(NY).date()


def vin(n: int) -> str:
    return f"1HGCM8263{n:08d}"


async def new_patient(client, headers, n: int = 1, **kw) -> dict:
    """A patient with a saved home address. Returns {'id', 'address_id'}."""
    defaults = dict(first_name=f"Pat{n}", last_name=f"Test{n}", medicaid_id=f"MD-{n:06d}",
     mobility="ambulatory", home_address=HOME_ADDRESS)
    defaults.update(kw)   # caller-supplied overrides win
    payload = patient_payload(**defaults)
    r = await client.post("/patients", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    addr = (await client.get(f"/patients/{pid}/addresses", headers=headers)).json()[0]["id"]
    return {"id": pid, "address_id": addr}


async def new_driver(client, headers, n: int = 1, **kw) -> dict:
    defaults = dict(email=f"driver{n}@fleet.example.com", full_name=f"Driver {n}", employee_number=f"E-{n:03d}")
    defaults.update(kw)
    r = await client.post("/drivers", headers=headers, json=driver_payload(**defaults))
    assert r.status_code == 201, r.text
    return {"id": r.json()["id"], "email": r.json()["email"]}


async def new_vehicle(client, headers, n: int = 1, **kw) -> str:
    r = await client.post("/vehicles", headers=headers, json=vehicle_payload(
        unit_number=f"V-{n:02d}", vin=vin(n), plate=f"PL{n:04d}", **kw))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def new_facility(client, headers) -> str:
    r = await client.post("/facilities", headers=headers, json=facility_payload())
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def world(client, company: dict) -> dict:
    """patient + facility + one wheelchair-certified driver + one wheelchair van, ready for trips."""
    h = company["admin_headers"]
    return {"patient": await new_patient(client, h), "facility_id": await new_facility(client, h),
            "driver": await new_driver(client, h), "vehicle_id": await new_vehicle(client, h)}


def trip_body(patient: dict, facility_id: str, when: datetime, **kw) -> dict:
    return {"patient_id": patient["id"], "pickup": {"patient_address_id": patient["address_id"]},
            "dropoff": {"facility_id": facility_id}, "scheduled_pickup_at": when.isoformat(), **kw}


async def new_trip(client, headers, patient: dict, facility_id: str, when: datetime, **kw) -> dict:
    r = await client.post("/trips", headers=headers, json=trip_body(patient, facility_id, when, **kw))
    assert r.status_code == 201, r.text
    return r.json()[0]


async def assign(client, headers, trip_id: str, driver_id: str, vehicle_id: str):
    return await client.post(f"/trips/{trip_id}/assign", headers=headers,
                             json={"driver_id": driver_id, "vehicle_id": vehicle_id})


def codes(response, key: str = "errors") -> set[str]:
    return {i["code"] for i in response.json()["detail"][key]}


async def driver_headers(client, slug: str, email: str) -> dict[str, str]:
    return bearer((await login(client, slug, email))["access_token"])


async def sql(statement: str, **params):
    """Run raw SQL as the app role with tenant checks bypassed (for backdating clocks in tests)."""
    from sqlalchemy import text

    from app.core.db import open_tenant_session
    async with open_tenant_session(None, bypass=True) as s:
        result = await s.execute(text(statement), params)
        await s.commit()
        return result