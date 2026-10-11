import hashlib
import json
from datetime import date, timedelta

from sqlalchemy import text

from app.core.db import open_tenant_session
from tests.helpers import PW, bearer, make_company, make_user, patient_payload, staff_headers

HOME = {"line1": "1 Home St", "city": "Arlington", "state": "va", "postal_code": "22201"}


async def _setup(client, super_token):
    c = await make_company(client, super_token, "alpha")
    return c, c["admin_headers"]


async def test_sensitive_fields_are_masked_by_role(client, super_token):
    c, admin = await _setup(client, super_token)
    created = (await client.post("/patients", headers=admin, json=patient_payload())).json()
    assert created["date_of_birth"] == "1950-05-17" and created["medicaid_id"] == "AB-123456"
    assert created["notes"].startswith("Oxygen")

    disp = await staff_headers(client, c, "dispatcher")
    seen = (await client.get(f"/patients/{created['id']}", headers=disp)).json()
    assert seen["date_of_birth"] is None and seen["medicaid_id"] is None and seen["notes"] is None
    assert seen["medicaid_last4"] == "3456" and seen["is_minor"] is False

    bill = await staff_headers(client, c, "billing_staff")
    assert (await client.get(f"/patients/{created['id']}", headers=bill)).json()["medicaid_id"] == "AB-123456"

    fleet = await staff_headers(client, c, "fleet_manager")
    assert (await client.get("/patients", headers=fleet)).status_code == 403  # no business with PHI


async def test_phi_is_encrypted_in_the_database(client, super_token):
    c, admin = await _setup(client, super_token)
    await client.post("/patients", headers=admin, json=patient_payload())
    async with open_tenant_session(None, bypass=True) as s:
        row = (await s.execute(text(
            "SELECT date_of_birth_enc, medicaid_id_enc, notes_enc, medicaid_id_hash, medicaid_last4 FROM patients"))).one()
    dob, mid, notes, mhash, last4 = row
    assert "1950" not in dob and "AB-123456" not in mid and "oxygen" not in notes.lower()
    assert len(mhash) == 64 and mhash != hashlib.sha256(b"AB123456").hexdigest()  # keyed, not a plain hash
    assert last4 == "3456"


async def test_search_by_name_and_medicaid_blind_index(client, super_token):
    c, admin = await _setup(client, super_token)
    await client.post("/patients", headers=admin, json=patient_payload())
    await client.post("/patients", headers=admin, json=patient_payload(
        first_name="Grace", last_name="Hopper", medicaid_id="ZZ-999999"))
    names = lambda r: sorted(p["last_name"] for p in r.json())  # noqa: E731
    assert names(await client.get("/patients", headers=admin, params={"q": "love"})) == ["Lovelace"]
    assert names(await client.get("/patients", headers=admin, params={"q": "grace hop"})) == ["Hopper"]
    assert names(await client.get("/patients", headers=admin, params={"q": "%%"})) == []   # wildcard is literal
    for spelling in ("ab 123456", "AB123456", "ab-123456"):
        r = await client.get("/patients", headers=admin, params={"medicaid_id": spelling})
        assert names(r) == ["Lovelace"], spelling
    dup = await client.post("/patients", headers=admin, json=patient_payload(first_name="Ada2", medicaid_id="ab123456"))
    assert dup.status_code == 409


async def test_write_permissions_and_validation(client, super_token):
    c, admin = await _setup(client, super_token)
    pid = (await client.post("/patients", headers=admin, json=patient_payload())).json()["id"]
    disp = await staff_headers(client, c, "dispatcher")
    bill = await staff_headers(client, c, "billing_staff")
    assert (await client.patch(f"/patients/{pid}", headers=disp, json={"phone": "555-0999"})).status_code == 200
    assert (await client.patch(f"/patients/{pid}", headers=disp, json={"notes": "x"})).status_code == 403
    assert (await client.patch(f"/patients/{pid}", headers=disp, json={"date_of_birth": "1960-01-01"})).status_code == 403
    assert (await client.patch(f"/patients/{pid}", headers=bill, json={"phone": "1"})).status_code == 403
    assert (await client.patch(f"/patients/{pid}", headers=admin, json={"mobility": None})).status_code == 422
    assert (await client.post("/patients", headers=admin, json=patient_payload(date_of_birth="2999-01-01"))).status_code == 422
    changed = (await client.patch(f"/patients/{pid}", headers=admin, json={"medicaid_id": "NEW-777777"})).json()
    assert changed["medicaid_last4"] == "7777"
    r = await client.get("/patients", headers=admin, params={"medicaid_id": "NEW777777"})
    assert len(r.json()) == 1                                   # blind index was recomputed


async def test_phi_access_is_audited_without_leaking_phi(client, super_token):
    c, admin = await _setup(client, super_token)
    pid = (await client.post("/patients", headers=admin, json=patient_payload())).json()["id"]
    await client.get(f"/patients/{pid}", headers=admin)
    await client.get("/patients", headers=admin, params={"q": "Zorblax"})
    logs = (await client.get("/audit-logs", headers=admin, params={"limit": 200})).json()
    actions = {row["action"] for row in logs}
    assert {"patient.create", "patient.read", "patient.list"} <= actions
    blob = json.dumps(logs)
    for secret in ("Zorblax", "Lovelace", "123456", "1950"):
        assert secret not in blob, secret


async def test_archive_keeps_the_record(client, super_token):
    c, admin = await _setup(client, super_token)
    pid = (await client.post("/patients", headers=admin, json=patient_payload())).json()["id"]
    assert (await client.delete(f"/patients/{pid}", headers=admin)).status_code == 204
    assert (await client.get("/patients", headers=admin)).json() == []
    shown = (await client.get("/patients", headers=admin, params={"include_archived": True})).json()
    assert len(shown) == 1 and shown[0]["archived_at"]
    async with open_tenant_session(None, bypass=True) as s:
        assert await s.scalar(text("SELECT count(*) FROM patients")) == 1   # retained, not erased


async def test_is_minor_flag(client, super_token):
    c, admin = await _setup(client, super_token)
    kid = (date.today() - timedelta(days=365 * 10)).isoformat()
    r = (await client.post("/patients", headers=admin, json=patient_payload(
        first_name="Kid", medicaid_id=None, date_of_birth=kid))).json()
    disp = await staff_headers(client, c, "dispatcher")
    assert (await client.get(f"/patients/{r['id']}", headers=disp)).json()["is_minor"] is True


async def test_addresses_geocoding_and_primary_switching(client, super_token):
    c, admin = await _setup(client, super_token)
    p = (await client.post("/patients", headers=admin, json=patient_payload(home_address=HOME))).json()
    pid = p["id"]
    addrs = (await client.get(f"/patients/{pid}/addresses", headers=admin)).json()
    assert len(addrs) == 1 and addrs[0]["is_primary"] and addrs[0]["state"] == "VA"
    assert addrs[0]["geocode_status"] == "ok" and addrs[0]["latitude"] and addrs[0]["longitude"]

    manual = {"label": "dialysis", "line1": "9 Clinic Rd", "city": "Fairfax", "state": "VA",
              "postal_code": "22030", "latitude": 38.9, "longitude": -77.0, "is_primary": True}
    new = (await client.post(f"/patients/{pid}/addresses", headers=admin, json=manual)).json()
    assert new["geocode_status"] == "manual" and (new["latitude"], new["longitude"]) == (38.9, -77.0)
    addrs = (await client.get(f"/patients/{pid}/addresses", headers=admin)).json()
    assert [a["is_primary"] for a in addrs].count(True) == 1 and addrs[0]["label"] == "dialysis"

    before = addrs[1]["latitude"]
    moved = (await client.patch(f"/patients/{pid}/addresses/{addrs[1]['id']}", headers=admin, json={
        "address": {**HOME, "line1": "77 Other Ave"}})).json()
    assert moved["latitude"] != before                        # text changed -> re-geocoded
    assert (await client.delete(f"/patients/{pid}/addresses/{new['id']}", headers=admin)).status_code == 204
    assert len((await client.get(f"/patients/{pid}/addresses", headers=admin)).json()) == 1
    half = {**manual, "longitude": None}
    assert (await client.post(f"/patients/{pid}/addresses", headers=admin, json=half)).status_code == 422


async def test_portal_user_linking_rules(client, super_token):
    c, admin = await _setup(client, super_token)
    reg = await client.post("/auth/register", headers={"X-Tenant": "alpha"},
                            json={"email": "rider@example.com", "password": PW, "full_name": "Rider"})
    rider_id = reg.json()["id"]
    staff = await make_user(client, c, "disp@alpha.example.com", "dispatcher")
    ok = await client.post("/patients", headers=admin, json=patient_payload(user_id=rider_id))
    assert ok.status_code == 201 and ok.json()["user_id"] == rider_id
    again = await client.post("/patients", headers=admin, json=patient_payload(
        first_name="Other", medicaid_id="OT-123456", user_id=rider_id))
    assert again.status_code == 409                                   # one portal login = one patient
    bad = await client.post("/patients", headers=admin, json=patient_payload(
        first_name="X", medicaid_id=None, user_id=staff["id"]))
    assert bad.status_code == 422                                     # only passenger accounts
    assert bearer