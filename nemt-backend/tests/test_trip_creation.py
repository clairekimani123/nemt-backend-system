import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.db import SessionLocal, open_tenant_session
from tests.helpers import (
    at,
    bearer,
    login,
    make_company,
    new_driver,
    new_facility,
    new_patient,
    new_trip,
    staff_headers,
    trip_body,
    world,
)


async def _co(client, super_token, slug="alpha"):
    c = await make_company(client, super_token, slug)
    return c, c["admin_headers"]


async def test_create_trip_numbering_snapshot_and_encryption(client, super_token):
    c, h = await _co(client, super_token)
    w = await world(client, c)
    t = await new_trip(client, h, w["patient"], w["facility_id"], at(3), special_instructions="Gate code 4455; knock twice")
    assert t["trip_ref"] == "T-000001" and t["status"] == "scheduled" and t["leg"] == "outbound"
    assert t["pickup"]["line1"] == "1 Home St" and t["dropoff"]["line1"] == "100 Main St"
    assert t["pickup"]["latitude"] and t["dropoff"]["longitude"]          # both ends are on the map
    assert t["mobility"] == "ambulatory"                                  # copied from the patient
    assert t["estimated_duration_min"] == 45 and t["special_instructions"] == "Gate code 4455; knock twice"
    t2 = await new_trip(client, h, w["patient"], w["facility_id"], at(4))
    assert t2["trip_ref"] == "T-000002"

    async with open_tenant_session(None, bypass=True) as s:
        enc = await s.scalar(text("SELECT special_instructions_enc FROM trips WHERE trip_number = 1"))
    assert "Gate code" not in enc                                          # free text is encrypted at rest

    # the trip keeps the address it was booked with, even if the patient later moves
    addr = w["patient"]["address_id"]
    moved = await client.patch(f"/patients/{w['patient']['id']}/addresses/{addr}", headers=h, json={
        "address": {"line1": "999 New Rd", "city": "Arlington", "state": "VA", "postal_code": "22201"}})
    assert moved.status_code == 200
    again = (await client.get(f"/trips/{t['id']}", headers=h)).json()
    assert again["pickup"]["line1"] == "1 Home St"


async def test_creation_validation(client, super_token):
    c, h = await _co(client, super_token)
    w = await world(client, c)
    p, f = w["patient"], w["facility_id"]
    post = lambda body: client.post("/trips", headers=h, json=body)  # noqa: E731
    base = trip_body(p, f, at(3))

    assert (await post({**base, "scheduled_pickup_at": at(-1).isoformat()})).status_code == 422      # past
    assert (await post({**base, "appointment_at": at(3, 13).isoformat()})).status_code == 422        # appt before pickup
    assert (await post({**base, "scheduled_pickup_at": "2030-01-01T10:00:00"})).status_code == 422   # no timezone
    both = {**base, "pickup": {"patient_address_id": p["address_id"], "facility_id": f}}
    assert (await post(both)).status_code == 422                                                      # two sources
    other = await new_patient(client, h, n=2)
    stolen = {**base, "pickup": {"patient_address_id": other["address_id"]}}
    assert (await post(stolen)).status_code == 422                                                    # someone else's address
    half = {**base, "dropoff": {"address": {"line1": "5 X St", "city": "Fairfax", "state": "VA",
                                            "postal_code": "22030", "latitude": 38.9}}}
    assert (await post(half)).status_code == 422                                                      # lat without lng
    inline = {**base, "dropoff": {"address": {"line1": "5 X St", "city": "Fairfax", "state": "VA", "postal_code": "22030"}}}
    assert (await post(inline)).status_code == 201                                                    # geocoded for us

    await client.delete(f"/patients/{other['id']}", headers=h)
    assert (await post({**base, "patient_id": other["id"], "pickup": {"facility_id": f}})).status_code == 422  # archived


async def test_minor_must_have_escort_and_defaults(client, super_token):
    c, h = await _co(client, super_token)
    f = await new_facility(client, h)
    kid_dob = (datetime.now(UTC) - timedelta(days=365 * 10)).date().isoformat()
    kid = await new_patient(client, h, n=5, date_of_birth=kid_dob)
    no_escort = await client.post("/trips", headers=h, json=trip_body(kid, f, at(3), escort_count=0))
    assert no_escort.status_code == 422
    t = await new_trip(client, h, kid, f, at(3))                       # omitted -> defaults to 1 escort
    assert t["escort_count"] == 1
    needs = await new_patient(client, h, n=6, requires_escort=True)
    assert (await new_trip(client, h, needs, f, at(3)))["escort_count"] == 1


async def test_patient_cannot_be_in_two_places(client, super_token):
    c, h = await _co(client, super_token)
    w = await world(client, c)
    p, f = w["patient"], w["facility_id"]
    await new_trip(client, h, p, f, at(3, 14, 0))                      # 14:00 - 14:45
    clash = await client.post("/trips", headers=h, json=trip_body(p, f, at(3, 14, 30)))
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "patient_overlap"
    assert clash.json()["detail"]["trips"] == ["T-000001"]
    assert (await client.post("/trips", headers=h, json=trip_body(p, f, at(3, 14, 45)))).status_code == 201  # back-to-back is fine


async def test_round_trip_creates_two_linked_trips(client, super_token):
    c, h = await _co(client, super_token)
    w = await world(client, c)
    body = trip_body(w["patient"], w["facility_id"], at(3, 14, 0), round_trip={"return_pickup_at": at(3, 17).isoformat()})
    r = await client.post("/trips", headers=h, json=body)
    assert r.status_code == 201
    out, back = r.json()
    assert (out["leg"], back["leg"]) == ("outbound", "return")
    assert back["return_of_trip_id"] == out["id"] and (out["trip_number"], back["trip_number"]) == (1, 2)
    assert back["pickup"]["line1"] == out["dropoff"]["line1"] and back["dropoff"]["line1"] == out["pickup"]["line1"]
    early = trip_body(w["patient"], w["facility_id"], at(4, 14, 0), round_trip={"return_pickup_at": at(4, 14, 20).isoformat()})
    assert (await client.post("/trips", headers=h, json=early)).status_code == 422   # back before the first ride ends


async def test_permissions(client, super_token):
    c, h = await _co(client, super_token)
    w = await world(client, c)
    body = trip_body(w["patient"], w["facility_id"], at(3))
    disp = await staff_headers(client, c, "dispatcher")
    bill = await staff_headers(client, c, "billing_staff")
    fleet = await staff_headers(client, c, "fleet_manager")
    assert (await client.post("/trips", headers=bill, json=body)).status_code == 403
    assert (await client.post("/trips", headers=disp, json=body)).status_code == 201
    assert (await client.get("/trips", headers=bill)).status_code == 200            # billing may read
    assert (await client.get("/trips", headers=fleet)).status_code == 403
    d = await new_driver(client, h, n=9)
    drv = bearer((await login(client, "alpha", d["email"]))["access_token"])
    assert (await client.get("/trips", headers=drv)).status_code == 403


async def test_update_rules(client, super_token):
    c, h = await _co(client, super_token)
    w = await world(client, c)
    t = await new_trip(client, h, w["patient"], w["facility_id"], at(3))
    r = await client.patch(f"/trips/{t['id']}", headers=h, json={"scheduled_pickup_at": at(4, 9).isoformat(), "estimated_duration_min": 60})
    assert r.status_code == 200 and r.json()["ends_at"] == at(4, 10).isoformat().replace("+00:00", "Z")
    assert (await client.patch(f"/trips/{t['id']}", headers=h, json={"scheduled_pickup_at": None})).status_code == 422
    assert (await client.patch(f"/trips/{t['id']}", headers=h, json={"scheduled_pickup_at": at(-2).isoformat()})).status_code == 422
    await client.post(f"/trips/{t['id']}/status", headers=h, json={"status": "cancelled", "reason_code": "weather"})
    assert (await client.patch(f"/trips/{t['id']}", headers=h, json={"uses_oxygen": True})).status_code == 409


async def test_trip_numbers_are_unique_under_concurrency_and_per_company(client, super_token):
    c, h = await _co(client, super_token)
    f = await new_facility(client, h)
    patients = [await new_patient(client, h, n=i) for i in range(1, 6)]
    results = await asyncio.gather(*[client.post("/trips", headers=h, json=trip_body(p, f, at(3))) for p in patients])
    assert all(r.status_code == 201 for r in results)
    assert sorted(r.json()[0]["trip_number"] for r in results) == [1, 2, 3, 4, 5]
    other = await make_company(client, super_token, "bravo")                       # numbering restarts per company
    ow = await world(client, other)
    assert (await new_trip(client, other["admin_headers"], ow["patient"], ow["facility_id"], at(3)))["trip_number"] == 1


async def test_cross_tenant_isolation_for_trips(client, super_token):
    a, ha = await _co(client, super_token, "alpha")
    b, hb = await _co(client, super_token, "bravo")
    wa, wb = await world(client, a), await world(client, b)
    ta = await new_trip(client, ha, wa["patient"], wa["facility_id"], at(3))
    tb = await new_trip(client, hb, wb["patient"], wb["facility_id"], at(3))
    for path in (f"/trips/{ta['id']}", f"/trips/{ta['id']}/events"):
        assert (await client.get(path, headers=hb)).status_code == 404
    assert (await client.post(f"/trips/{ta['id']}/status", headers=hb, json={"status": "cancelled", "reason_code": "weather"})).status_code == 404
    assert (await client.post(f"/trips/{ta['id']}/assign", headers=hb, json={"driver_id": wb["driver"]["id"], "vehicle_id": wb["vehicle_id"]})).status_code == 404
    assert [x["id"] for x in (await client.get("/trips", headers=hb)).json()] == [tb["id"]]
    # B cannot book a trip for A's patient, nor use A's saved address / facility
    assert (await client.post("/trips", headers=hb, json=trip_body(wa["patient"], wb["facility_id"], at(5)))).status_code == 404

    await client.post("/recurring-trips", headers=ha, json={
        "patient_id": wa["patient"]["id"], "pickup": {"patient_address_id": wa["patient"]["address_id"]},
        "dropoff": {"facility_id": wa["facility_id"]}, "rrule": "FREQ=DAILY;COUNT=2",
        "start_date": (datetime.now(UTC) + timedelta(days=5)).date().isoformat(), "pickup_time": "08:00:00", "materialize_now": False})
    await client.post("/recurring-trips", headers=hb, json={
        "patient_id": wb["patient"]["id"], "pickup": {"patient_address_id": wb["patient"]["address_id"]},
        "dropoff": {"facility_id": wb["facility_id"]}, "rrule": "FREQ=DAILY;COUNT=2",
        "start_date": (datetime.now(UTC) + timedelta(days=5)).date().isoformat(), "pickup_time": "08:00:00", "materialize_now": False})
    ca, cb = uuid.UUID(a["company"]["id"]), uuid.UUID(b["company"]["id"])
    for table in ("trips", "trip_events", "recurring_trips", "counters"):
        async with open_tenant_session(ca) as s:
            mine = await s.scalar(text(f"SELECT count(*) FROM {table}"))
            theirs = await s.scalar(text(f"SELECT count(*) FROM {table} WHERE company_id = :c"), {"c": cb})
            assert mine >= 1 and theirs == 0, table
        async with SessionLocal() as s:   # no tenant at all -> fail closed
            assert await s.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table