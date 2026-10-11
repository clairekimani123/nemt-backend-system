"""The trip life-cycle: driver taps, cancellations, no-shows, lateness, and what the database refuses."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import open_tenant_session
from tests.helpers import (
    assign,
    at,
    driver_headers,
    local_date,
    make_company,
    new_driver,
    new_facility,
    new_patient,
    new_trip,
    new_vehicle,
    sql,
)

PATH = ["en_route_to_pickup", "arrived_at_pickup", "passenger_on_board", "en_route_to_dropoff",
        "arrived_at_dropoff", "completed"]


async def _ready(client, super_token, assigned=True, when=None, patients=1):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    f = await new_facility(client, h)
    ps = [await new_patient(client, h, n=i) for i in range(1, patients + 1)]
    d, v = await new_driver(client, h), await new_vehicle(client, h)
    t = await new_trip(client, h, ps[0], f, when or at(3))
    if assigned:
        assert (await assign(client, h, t["id"], d["id"], v)).status_code == 200
    return c, h, f, ps, d, v, t, await driver_headers(client, "alpha", d["email"])


def step(client, hd, trip_id, status, **extra):
    return client.post(f"/driver/trips/{trip_id}/status", headers=hd, json={"status": status, **extra})


async def test_full_driver_workflow_records_timestamps_and_gps(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token)
    for s in PATH:
        r = await step(client, hd, t["id"], s, latitude=38.9, longitude=-77.0)
        assert r.status_code == 200 and r.json()["status"] == s, r.text
    done = (await client.get(f"/trips/{t['id']}", headers=h)).json()
    for stamp in ("en_route_at", "arrived_pickup_at", "picked_up_at", "arrived_dropoff_at", "completed_at"):
        assert done[stamp], stamp
    events = (await client.get(f"/trips/{t['id']}/events", headers=h)).json()
    assert [e["to_status"] for e in events] == ["scheduled", "assigned", *PATH]
    assert all(e["latitude"] == 38.9 and e["longitude"] == -77.0 for e in events[2:])   # proof of service
    assert (await step(client, hd, t["id"], "en_route_to_pickup")).status_code == 409    # completed = final


async def test_illegal_moves_are_refused(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token)
    fresh = await new_trip(client, h, ps[0], f, at(5))                                   # still unassigned
    assert (await client.post(f"/trips/{fresh['id']}/status", headers=h, json={"status": "completed"})).status_code == 409
    assert (await client.post(f"/trips/{t['id']}/status", headers=h, json={"status": "assigned"})).status_code == 422
    assert (await step(client, hd, t["id"], "completed")).status_code == 409             # can't skip ahead
    assert (await step(client, hd, t["id"], "passenger_on_board")).status_code == 409
    r = await step(client, hd, t["id"], "cancelled", reason_code="weather")
    assert r.status_code == 403                                                          # drivers can't cancel

    other = await new_driver(client, h, n=2)
    ho = await driver_headers(client, "alpha", other["email"])
    assert (await step(client, ho, t["id"], "en_route_to_pickup")).status_code == 404    # not their trip
    assert (await client.get(f"/driver/trips/{t['id']}", headers=ho)).status_code == 404
    assert (await client.get("/driver/trips", headers=h)).status_code == 403             # dispatch has no driver app


async def test_cancellation_rules_and_late_cancel_flag(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token)
    assert (await client.post(f"/trips/{t['id']}/status", headers=h, json={"status": "cancelled"})).status_code == 422
    r = await client.post(f"/trips/{t['id']}/status", headers=h, json={"status": "cancelled", "reason_code": "patient_cancelled"})
    assert r.status_code == 200 and r.json()["cancel_reason_code"] == "patient_cancelled" and r.json()["late_cancel"] is False
    assert (await client.post(f"/trips/{t['id']}/status", headers=h, json={"status": "cancelled", "reason_code": "weather"})).status_code == 409

    soon = await new_trip(client, h, ps[0], f, datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1))
    r = await client.post(f"/trips/{soon['id']}/status", headers=h, json={"status": "cancelled", "reason_code": "patient_cancelled"})
    assert r.json()["late_cancel"] is True                                               # cancelled < 2h before pickup

    onboard = await new_trip(client, h, ps[0], f, at(6))
    await assign(client, h, onboard["id"], d["id"], v)
    for s in PATH[:3]:
        await step(client, hd, onboard["id"], s)
    r = await client.post(f"/trips/{onboard['id']}/status", headers=h, json={"status": "cancelled", "reason_code": "other"})
    assert r.status_code == 409                                                          # patient already in the vehicle


async def test_no_show_rules(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token, patients=2)
    await step(client, hd, t["id"], "en_route_to_pickup")
    await step(client, hd, t["id"], "arrived_at_pickup")
    assert (await step(client, hd, t["id"], "no_show")).status_code == 409               # must wait first
    await sql("UPDATE trips SET arrived_pickup_at = now() - interval '11 minutes' WHERE id = :i", i=uuid.UUID(t["id"]))
    r = await step(client, hd, t["id"], "no_show")
    assert r.status_code == 200 and r.json()["status"] == "no_show"

    t2 = await new_trip(client, h, ps[1], f, at(4))                                      # dispatch may call it at once
    await assign(client, h, t2["id"], d["id"], v)
    await step(client, hd, t2["id"], "en_route_to_pickup")
    await step(client, hd, t2["id"], "arrived_at_pickup")
    r = await client.post(f"/trips/{t2['id']}/status", headers=h, json={"status": "no_show", "note": "called twice, no answer"})
    assert r.status_code == 200 and r.json()["status"] == "no_show"
    async with open_tenant_session(None, bypass=True) as s:                               # the note is encrypted
        enc = await s.scalar(text("SELECT note_enc FROM trip_events WHERE note_enc IS NOT NULL LIMIT 1"))
    assert "called twice" not in enc


async def test_late_and_at_risk_flags(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token, assigned=False, when=datetime.now(UTC) + timedelta(hours=1))
    first = (await client.get(f"/trips/{t['id']}", headers=h)).json()
    assert first["at_risk"] is True and first["is_late"] is False                         # unassigned, pickup within 2h

    await sql("UPDATE trips SET scheduled_pickup_at = now() - interval '30 minutes', ends_at = now() + interval '15 minutes'"
              " WHERE id = :i", i=uuid.UUID(t["id"]))
    late = (await client.get(f"/trips/{t['id']}", headers=h)).json()
    assert late["is_late"] is True and late["at_risk"] is False
    assert [x["id"] for x in (await client.get("/trips", headers=h, params={"late": True})).json()] == [t["id"]]
    board = (await client.get("/dispatch/board", headers=h, params={"on_date": local_date(datetime.now(UTC) - timedelta(minutes=30)).isoformat()})).json()
    assert board["late"] >= 1

    assert (await assign(client, h, t["id"], d["id"], v)).status_code == 200
    hd2 = await driver_headers(client, "alpha", d["email"])
    await step(client, hd2, t["id"], "en_route_to_pickup")
    assert (await client.get(f"/trips/{t['id']}", headers=h)).json()["is_late"] is True   # still not there
    await step(client, hd2, t["id"], "arrived_at_pickup")
    assert (await client.get(f"/trips/{t['id']}", headers=h)).json()["is_late"] is False  # arrived: no longer "late"


async def test_audit_trail_has_no_phi(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token)
    await client.get(f"/trips/{t['id']}", headers=h)
    await step(client, hd, t["id"], "en_route_to_pickup")
    logs = (await client.get("/audit-logs", headers=h, params={"limit": 200})).json()
    actions = {row["action"] for row in logs}
    assert {"trip.create", "trip.assign", "trip.read", "trip.status"} <= actions
    blob = json.dumps(logs)
    for secret in ("Test1", "Home St", "Main St"):
        assert secret not in blob, secret


async def test_database_refuses_impossible_trips(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token)
    tid = uuid.UUID(t["id"])
    async with open_tenant_session(None, bypass=True) as s:
        with pytest.raises(IntegrityError, match="active_trip_needs_driver"):
            await s.execute(text("UPDATE trips SET status='en_route_to_pickup', driver_id=NULL, vehicle_id=NULL WHERE id=:i"), {"i": tid})
        await s.rollback()
        with pytest.raises(IntegrityError, match="driver_and_vehicle_together"):
            await s.execute(text("UPDATE trips SET vehicle_id=NULL WHERE id=:i"), {"i": tid})
        await s.rollback()
        with pytest.raises(IntegrityError, match="cancel_needs_reason"):
            await s.execute(text("UPDATE trips SET status='cancelled' WHERE id=:i"), {"i": tid})
        await s.rollback()
        with pytest.raises(IntegrityError, match="scheduled_has_no_assignment"):
            await s.execute(text("UPDATE trips SET status='scheduled' WHERE id=:i"), {"i": tid})
        await s.rollback()
        with pytest.raises(IntegrityError, match="ends_after_pickup"):
            await s.execute(text("UPDATE trips SET ends_at = scheduled_pickup_at WHERE id=:i"), {"i": tid})
        await s.rollback()
        with pytest.raises(DBAPIError, match="permission denied"):                        # history is append-only
            await s.execute(text("UPDATE trip_events SET event_type = 'tampered'"))
        await s.rollback()
        with pytest.raises(DBAPIError, match="permission denied"):
            await s.execute(text("DELETE FROM trip_events"))
        await s.rollback()
        with pytest.raises(DBAPIError, match="permission denied"):                        # trips are never deleted
            await s.execute(text("DELETE FROM trips"))


async def test_driver_app_shows_only_what_a_driver_needs(client, super_token):
    c, h, f, ps, d, v, t, hd = await _ready(client, super_token)
    unassigned = await new_trip(client, h, ps[0], f, at(5))
    day = local_date(at(3)).isoformat()
    r = await client.get("/driver/trips", headers=hd, params={"on_date": day})
    mine = r.json()
    assert [x["id"] for x in mine] == [t["id"]]                                          # unassigned trips never appear
    assert unassigned["id"] not in json.dumps(mine)
    trip = mine[0]
    assert trip["patient_first_name"] == "Pat1" and trip["pickup"]["line1"] == "1 Home St" and trip["vehicle_unit"] == "V-01"
    for forbidden in ("date_of_birth", "medicaid_id", "medicaid_last4", "broker_id", "authorization_number", "notes"):
        assert forbidden not in trip
    assert (await client.get("/driver/trips", headers=hd, params={"on_date": local_date(at(9)).isoformat()})).json() == []