"""Tenant isolation for every Phase 2 table: API level AND database level."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import SessionLocal, open_tenant_session
from tests.helpers import (
    bearer,
    driver_payload,
    facility_payload,
    make_company,
    patient_payload,
    upload,
    vehicle_payload,
)

TABLES = ["patients", "patient_addresses", "drivers", "vehicles", "facilities", "brokers", "documents"]
HOME = {"line1": "1 Home St", "city": "Arlington", "state": "VA", "postal_code": "22201"}


async def seed(client, company: dict) -> dict:
    h, slug = company["admin_headers"], company["slug"]
    ids = {}
    ids["patient"] = (await client.post("/patients", headers=h, json=patient_payload(home_address=HOME))).json()["id"]
    ids["driver"] = (await client.post("/drivers", headers=h, json=driver_payload(f"d@{slug}.example.com"))).json()["id"]
    ids["vehicle"] = (await client.post("/vehicles", headers=h, json=vehicle_payload())).json()["id"]
    ids["facility"] = (await client.post("/facilities", headers=h, json=facility_payload())).json()["id"]
    ids["broker"] = (await client.post("/brokers", headers=h, json={"name": "ModivCare", "code": "modivcare"})).json()["id"]
    r = await upload(client, h, "patient", ids["patient"])
    assert r.status_code == 201, r.text
    ids["document"] = r.json()["id"]
    return ids


@pytest.fixture
async def two(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    return a, b, await seed(client, a), await seed(client, b)


async def test_other_tenants_records_are_404_everywhere(client, two):
    a, b, ids_a, _ = two
    hb = b["admin_headers"]
    for path in ("patients", "drivers", "vehicles", "facilities", "brokers"):
        rid = ids_a[path.rstrip("s").replace("facilitie", "facility")]
        assert (await client.get(f"/{path}/{rid}", headers=hb)).status_code == 404, path
        assert (await client.patch(f"/{path}/{rid}", headers=hb, json={})).status_code == 404, path
        assert (await client.delete(f"/{path}/{rid}", headers=hb)).status_code == 404, path
    assert (await client.get(f"/patients/{ids_a['patient']}/addresses", headers=hb)).status_code == 404
    assert (await client.get(f"/documents/{ids_a['document']}/download", headers=hb)).status_code == 404
    assert (await upload(client, hb, "patient", ids_a["patient"])).status_code == 404  # can't attach to A's patient
    assert (await client.delete(f"/documents/{ids_a['document']}", headers=hb)).status_code == 404


async def test_lists_only_contain_own_tenant(client, two):
    a, b, ids_a, ids_b = two
    for path, key in (("patients", "patient"), ("drivers", "driver"), ("vehicles", "vehicle"),
                      ("facilities", "facility"), ("brokers", "broker")):
        got = {row["id"] for row in (await client.get(f"/{path}", headers=b["admin_headers"])).json()}
        assert got == {ids_b[key]}, path


@pytest.mark.parametrize("table", TABLES)
async def test_rls_on_every_table(two, table):
    a, b, _, _ = two
    ca, cb = uuid.UUID(a["company"]["id"]), uuid.UUID(b["company"]["id"])
    async with open_tenant_session(ca) as s:
        assert await s.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        assert await s.scalar(text(f"SELECT count(*) FROM {table} WHERE company_id = :c"), {"c": cb}) == 0
    async with SessionLocal() as s:  # no tenant context at all -> fail closed
        assert await s.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_app_role_cannot_hard_delete_phi(two):
    a, *_ = two
    async with open_tenant_session(uuid.UUID(a["company"]["id"])) as s:
        for table in ("patients", "drivers", "vehicles", "facilities", "brokers", "documents"):
            with pytest.raises(DBAPIError, match="permission denied"):
                await s.execute(text(f"DELETE FROM {table}"))
            await s.rollback()
        # an address typo may be deleted; that table is not a medical record
        assert (await s.execute(text("DELETE FROM patient_addresses"))).rowcount == 1


async def test_document_owner_rules_enforced_by_database(two):
    a, b, ids_a, ids_b = two
    ca = uuid.UUID(a["company"]["id"])
    insert = text("""INSERT INTO documents (id, company_id, patient_id, driver_id, doc_type, filename,
                     content_type, size_bytes, sha256, storage_key)
                     VALUES (gen_random_uuid(), :c, :p, :d, 'other', 'f.pdf', 'application/pdf', 1, 'x',
                     gen_random_uuid()::text)""")
    async with open_tenant_session(None, bypass=True) as s:
        with pytest.raises(IntegrityError, match="exactly_one_owner"):  # two owners
            await s.execute(insert, {"c": ca, "p": uuid.UUID(ids_a["patient"]), "d": uuid.UUID(ids_a["driver"])})
        await s.rollback()
        with pytest.raises(IntegrityError, match="exactly_one_owner"):  # no owner
            await s.execute(insert, {"c": ca, "p": None, "d": None})
        await s.rollback()
        with pytest.raises(IntegrityError, match="fk_documents_company_patient"):  # A's company, B's patient
            await s.execute(insert, {"c": ca, "p": uuid.UUID(ids_b["patient"]), "d": None})


async def test_same_medicaid_id_allowed_in_different_tenants(two):
    """Both tenants seeded a patient with the same Medicaid ID; uniqueness is per company."""
    a, b, ids_a, ids_b = two
    assert ids_a["patient"] != ids_b["patient"]


async def test_vehicle_cannot_reference_other_tenants_driver(client, two):
    a, b, ids_a, ids_b = two
    r = await client.post("/vehicles", headers=a["admin_headers"], json=vehicle_payload(
        unit_number="V-99", vin="2HGCM82633A004353", default_driver_id=ids_b["driver"]))
    assert r.status_code == 409
    r = await client.post("/vehicles", headers=a["admin_headers"], json=vehicle_payload(
        unit_number="V-98", vin="3HGCM82633A004354", default_driver_id=ids_a["driver"]))
    assert r.status_code == 201 and bearer  # own driver is fine