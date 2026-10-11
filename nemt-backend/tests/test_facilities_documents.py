import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.config import settings
from tests.helpers import (
    PDF,
    bearer,
    driver_payload,
    facility_payload,
    make_company,
    patient_payload,
    staff_headers,
    upload,
    vehicle_payload,
)


def _fac(name: str, lat: float, lng: float, kind: str = "clinic") -> dict:
    a = {"line1": f"{name} Way", "city": "Arlington", "state": "VA", "postal_code": "22201",
         "latitude": lat, "longitude": lng}
    return facility_payload(name=name, kind=kind, address=a)


def _today():
    return datetime.now(UTC).date()


# ------------------------------ facilities + PostGIS ------------------------------
async def test_facility_geocoding_and_search(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    f = (await client.post("/facilities", headers=h, json=facility_payload())).json()
    assert f["state"] == "VA" and f["geocode_status"] == "ok" and f["latitude"] and f["longitude"]
    assert [x["name"] for x in (await client.get("/facilities", headers=h, params={"q": "riverside"})).json()] == ["Riverside Dialysis"]
    assert (await client.get("/facilities", headers=h, params={"kind": "hospital"})).json() == []
    disp = await staff_headers(client, c, "dispatcher")
    assert (await client.get("/facilities", headers=disp)).status_code == 200
    assert (await client.post("/facilities", headers=disp, json=facility_payload())).status_code == 403
    assert (await client.patch(f"/facilities/{f['id']}", headers=h, json={"name": None})).status_code == 422


async def test_nearby_uses_real_world_distance(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    for fac in (_fac("Here", 38.9000, -77.0000), _fac("FiveKmNorth", 38.9500, -77.0000),
                _fac("NewYork", 40.7128, -74.0060, "hospital")):
        assert (await client.post("/facilities", headers=h, json=fac)).status_code == 201

    def near(**p):
        return client.get("/facilities/nearby", headers=h, params={"lat": 38.9, "lng": -77.0, **p})

    got = (await near(radius_km=10)).json()
    assert [x["name"] for x in got] == ["Here", "FiveKmNorth"]                 # nearest first, NYC excluded
    assert got[0]["distance_m"] < 1 and 5500 < got[1]["distance_m"] < 5620     # 0.05 deg of latitude ~ 5.56 km
    assert [x["name"] for x in (await near(radius_km=3)).json()] == ["Here"]
    assert {x["name"] for x in (await near(radius_km=200)).json()} == {"Here", "FiveKmNorth"}   # NYC is ~330 km away
    assert (await near(radius_km=500)).status_code == 422                                      # API caps the radius at 200 km
    nyc = {"lat": 40.7128, "lng": -74.0060, "radius_km": 5}
    assert [x["name"] for x in (await client.get("/facilities/nearby", headers=h, params={**nyc, "kind": "hospital"})).json()] == ["NewYork"]
    assert (await client.get("/facilities/nearby", headers=h, params={**nyc, "kind": "clinic"})).json() == []
    assert (await client.get("/facilities/nearby", headers=h, params={"lat": 95, "lng": 0})).status_code == 422

    other = await make_company(client, super_token, "bravo")                    # B sees none of A's
    r = await client.get("/facilities/nearby", headers=other["admin_headers"], params={"lat": 38.9, "lng": -77.0, "radius_km": 200})
    assert r.json() == []

    here_id = got[0]["id"]
    await client.delete(f"/facilities/{here_id}", headers=h)                   # archived places drop out
    assert [x["name"] for x in (await near(radius_km=10)).json()] == ["FiveKmNorth"]


async def test_brokers_crud(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    b = (await client.post("/brokers", headers=h, json={"name": "ModivCare", "code": "modivcare"})).json()
    assert b["code"] == "MODIVCARE"
    assert (await client.post("/brokers", headers=h, json={"name": "Dup", "code": "ModivCare"})).status_code == 409
    assert (await client.patch(f"/brokers/{b['id']}", headers=h, json={"phone": "800-555-0100"})).json()["phone"] == "800-555-0100"
    assert (await client.delete(f"/brokers/{b['id']}", headers=h)).status_code == 204
    assert (await client.get("/brokers", headers=h)).json() == []


# ------------------------------ documents ------------------------------
async def test_upload_download_roundtrip_and_encryption_at_rest(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    pid = (await client.post("/patients", headers=h, json=patient_payload())).json()["id"]
    r = await upload(client, h, "patient", pid, filename="../../etc/passwd.pdf")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["owner_type"] == "patient" and doc["owner_id"] == pid
    assert doc["filename"] == "passwd.pdf" and "storage_key" not in doc       # client path never trusted / exposed

    dl = await client.get(f"/documents/{doc['id']}/download", headers=h)
    assert dl.status_code == 200 and dl.content == PDF
    assert dl.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in dl.headers["content-security-policy"]

    files = [p for p in Path(settings.storage_dir).rglob("*.bin")]
    assert files, "expected an encrypted file on disk"
    assert all(b"SECRET-MARKER-12345" not in p.read_bytes() and b"%PDF" not in p.read_bytes() for p in files)


async def test_upload_rejections(client, super_token, monkeypatch):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    pid = (await client.post("/patients", headers=h, json=patient_payload())).json()["id"]
    assert (await upload(client, h, "patient", pid, data=b"hello", ctype="text/plain", filename="a.txt")).status_code == 415
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    assert (await upload(client, h, "patient", pid, data=png, ctype="image/png", filename="a.png")).status_code == 201
    assert (await upload(client, h, "patient", pid, data=png, ctype="application/pdf")).status_code == 415   # lies about type
    assert (await upload(client, h, "patient", pid, data=b"")).status_code == 400
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    big = b"%PDF-" + b"0" * (1024 * 1024)
    assert (await upload(client, h, "patient", pid, data=big)).status_code == 413


async def test_document_permissions_by_owner_type(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    pid = (await client.post("/patients", headers=h, json=patient_payload())).json()["id"]
    vid = (await client.post("/vehicles", headers=h, json=vehicle_payload())).json()["id"]
    disp = await staff_headers(client, c, "dispatcher")
    fleet = await staff_headers(client, c, "fleet_manager")
    bill = await staff_headers(client, c, "billing_staff")
    assert (await upload(client, disp, "patient", pid)).status_code == 201     # dispatcher: patients:write
    assert (await upload(client, disp, "vehicle", vid)).status_code == 403
    assert (await upload(client, fleet, "vehicle", vid)).status_code == 201     # fleet manager: vehicles:write
    assert (await upload(client, fleet, "patient", pid)).status_code == 403
    assert (await upload(client, bill, "patient", pid)).status_code == 403      # billing reads, never writes
    docs = (await client.get("/documents", headers=bill, params={"owner_type": "patient", "owner_id": pid})).json()
    assert len(docs) == 1
    assert (await client.get(f"/documents/{docs[0]['id']}/download", headers=bill)).status_code == 200
    assert (await client.get("/documents", headers=fleet, params={"owner_type": "patient", "owner_id": pid})).status_code == 403


async def test_document_audit_and_archive(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    pid = (await client.post("/patients", headers=h, json=patient_payload())).json()["id"]
    doc = (await upload(client, h, "patient", pid)).json()
    await client.get(f"/documents/{doc['id']}/download", headers=h)
    logs = (await client.get("/audit-logs", headers=h, params={"limit": 200})).json()
    assert {"document.upload", "document.download"} <= {row["action"] for row in logs}
    assert "SECRET-MARKER" not in json.dumps(logs)
    assert (await client.delete(f"/documents/{doc['id']}", headers=h)).status_code == 204
    assert (await client.get("/documents", headers=h, params={"owner_type": "patient", "owner_id": pid})).json() == []
    kept = await client.get("/documents", headers=h, params={"owner_type": "patient", "owner_id": pid, "include_archived": True})
    assert len(kept.json()) == 1                                                # retained for the retention period


# ------------------------------ compliance ------------------------------
async def test_expiration_report(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    t = _today()
    iso = lambda days: (t + timedelta(days=days)).isoformat()  # noqa: E731
    await client.post("/drivers", headers=h, json=driver_payload(license_expires_on=iso(10), medical_cert_expires_on=iso(-3)))
    vid = (await client.post("/vehicles", headers=h, json=vehicle_payload(
        insurance_expires_on=iso(-5), registration_expires_on=iso(200)))).json()["id"]
    await upload(client, h, "vehicle", vid, doc_type="inspection", expires_on=iso(5))
    await upload(client, h, "vehicle", vid, doc_type="insurance", expires_on=iso(100))

    soon = (await client.get("/compliance/expirations", headers=h, params={"days": 30})).json()
    assert [i["kind"] for i in soon] == ["vehicle_insurance", "driver_medical_cert", "document", "driver_license"]
    assert [i["days_left"] for i in soon] == [-5, -3, 5, 10]                    # sorted soonest first, negative = expired
    wide = (await client.get("/compliance/expirations", headers=h, params={"days": 365})).json()
    assert {i["kind"] for i in wide} >= {"vehicle_registration"} and len(wide) == 6

    disp = await staff_headers(client, c, "dispatcher")
    fleet = await staff_headers(client, c, "fleet_manager")
    assert (await client.get("/compliance/expirations", headers=disp)).status_code == 403
    assert (await client.get("/compliance/expirations", headers=fleet)).status_code == 200
    other = await make_company(client, super_token, "bravo")
    assert (await client.get("/compliance/expirations", headers=other["admin_headers"], params={"days": 365})).json() == []
    assert bearer