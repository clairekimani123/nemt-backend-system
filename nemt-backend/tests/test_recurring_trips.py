"""Standing orders ('dialysis Mon/Wed/Fri 07:30') and the real trips cooked from them."""

import uuid
from datetime import UTC, datetime, time, timedelta

from app.modules.trips.jobs import materialize_all
from tests.helpers import (
    NY,
    at_local,
    bearer,
    make_company,
    new_trip,
    new_vehicle,
    sql,
    staff_headers,
    world,
)

HORIZON = 30


def ny_today():
    return datetime.now(NY).date()


def series_body(w: dict, start, **kw) -> dict:
    return {"patient_id": w["patient"]["id"], "pickup": {"patient_address_id": w["patient"]["address_id"]},
            "dropoff": {"facility_id": w["facility_id"]}, "rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
            "start_date": start.isoformat(), "pickup_time": "07:30:00", **kw}


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def _all_trips(client, h, **params):
    return (await client.get("/trips", headers=h, params={"limit": 100, **params})).json()


async def _setup(client, super_token, slug="alpha"):
    c = await make_company(client, super_token, slug)
    return c, c["admin_headers"], await world(client, c)


async def test_series_generates_linked_round_trips_idempotently(client, super_token):
    c, h, w = await _setup(client, super_token)
    start = ny_today() + timedelta(days=1)
    r = await client.post("/recurring-trips", headers=h, json=series_body(w, start, return_pickup_time="11:00:00"))
    assert r.status_code == 201, r.text
    series_id, generated = r.json()["series"]["id"], r.json()["generated"]

    end = datetime.now(UTC).date() + timedelta(days=HORIZON)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    expected = [d for d in days if d.weekday() in (0, 2, 4)]
    assert generated["created"] == 2 * len(expected) and generated["skipped"] == []

    trips = await _all_trips(client, h)
    assert len(trips) == 2 * len(expected) and all(t["series_id"] == series_id and t["source"] == "recurring" for t in trips)
    outbound = {parse(t["scheduled_pickup_at"]).astimezone(NY).date(): t for t in trips if t["leg"] == "outbound"}
    for t in trips:
        local = parse(t["scheduled_pickup_at"]).astimezone(NY)
        assert local.time() == (time(7, 30) if t["leg"] == "outbound" else time(11, 0))
        if t["leg"] == "return":
            assert t["return_of_trip_id"] == outbound[local.date()]["id"]
    again = await client.post(f"/recurring-trips/{series_id}/materialize", headers=h)
    assert again.status_code == 200 and again.json()["created"] == 0                       # safe to run twice


async def test_wall_clock_time_survives_daylight_saving(client, super_token):
    c, h, w = await _setup(client, super_token)

    def offset(day):
        return datetime.combine(day, time(12), tzinfo=NY).utcoffset()

    chg = ny_today() + timedelta(days=3)
    while offset(chg) == offset(chg - timedelta(days=1)):
        chg += timedelta(days=1)                                                              # first clock change ahead
    start = chg - timedelta(days=1)
    r = await client.post("/recurring-trips", headers=h, json=series_body(
        w, start, rrule="FREQ=DAILY;COUNT=3", materialize_now=False))
    sid = r.json()["series"]["id"]
    m = await client.post(f"/recurring-trips/{sid}/materialize", headers=h, params={"until": (chg + timedelta(days=2)).isoformat()})
    assert m.json()["created"] == 3, m.text
    trips = sorted(await _all_trips(client, h), key=lambda t: t["scheduled_pickup_at"])
    assert [parse(t["scheduled_pickup_at"]).astimezone(NY).time() for t in trips] == [time(7, 30)] * 3   # always 07:30 local
    hours = [parse(t["scheduled_pickup_at"]).hour for t in trips]
    assert abs(hours[0] - hours[2]) == 1                                                      # ...so the UTC hour moved


async def test_rule_validation(client, super_token):
    c, h, w = await _setup(client, super_token)
    start = ny_today() + timedelta(days=1)
    for bad in ("FREQ=SECONDLY", "FREQ=DAILY;COUNT=100000", "garbage", "FREQ=DAILY;UNTIL=20300101",
                "FREQ=WEEKLY;BYDAY=XX", "FREQ=DAILY;BYSECOND=5", "FREQ=DAILY;INTERVAL=0"):
        r = await client.post("/recurring-trips", headers=h, json=series_body(w, start, rrule=bad))
        assert r.status_code == 422, bad
    too_early = series_body(w, start, return_pickup_time="08:00:00")                           # before the first ride ends
    assert (await client.post("/recurring-trips", headers=h, json=too_early)).status_code == 422
    assert (await client.post("/recurring-trips", headers=h, json=series_body(w, start, pickup_time="07:30:00+05:00"))).status_code == 422
    assert (await client.post("/recurring-trips", headers=h, json=series_body(w, start, end_date=(start - timedelta(days=1)).isoformat()))).status_code == 422


async def test_cancelled_trip_is_not_recreated(client, super_token):
    c, h, w = await _setup(client, super_token)
    start = ny_today() + timedelta(days=1)
    r = await client.post("/recurring-trips", headers=h, json=series_body(w, start, rrule="FREQ=DAILY;COUNT=3"))
    sid = r.json()["series"]["id"]
    trips = sorted(await _all_trips(client, h), key=lambda t: t["scheduled_pickup_at"])
    assert len(trips) == 3
    await client.post(f"/trips/{trips[0]['id']}/status", headers=h, json={"status": "cancelled", "reason_code": "patient_hospitalized"})
    await sql("UPDATE recurring_trips SET materialized_through = NULL WHERE id = :i", i=uuid.UUID(sid))   # force a re-run from scratch
    again = await client.post(f"/recurring-trips/{sid}/materialize", headers=h)
    assert again.json()["created"] == 0
    after = await _all_trips(client, h)
    assert len(after) == 3 and sum(t["status"] == "cancelled" for t in after) == 1


async def test_ending_a_series_cancels_only_the_future(client, super_token):
    c, h, w = await _setup(client, super_token)
    start = ny_today() + timedelta(days=1)
    r = await client.post("/recurring-trips", headers=h, json=series_body(w, start, rrule="FREQ=DAILY", return_pickup_time="11:00:00"))
    sid = r.json()["series"]["id"]
    cut = start + timedelta(days=7)
    res = await client.post(f"/recurring-trips/{sid}/end", headers=h, json={"from_date": cut.isoformat(), "reason_code": "patient_hospitalized"})
    trips = await _all_trips(client, h)
    later = [t for t in trips if parse(t["scheduled_pickup_at"]).astimezone(NY).date() >= cut]
    assert res.json()["cancelled_trips"] == len(later) > 0
    assert all(t["status"] == "cancelled" and t["cancel_reason_code"] == "patient_hospitalized" for t in later)
    assert all(t["status"] == "scheduled" for t in trips if t not in later)
    s = (await client.get(f"/recurring-trips/{sid}", headers=h)).json()
    assert s["end_date"] == (cut - timedelta(days=1)).isoformat()
    assert (await client.post(f"/recurring-trips/{sid}/materialize", headers=h, params={"until": (cut + timedelta(days=20)).isoformat()})).json()["created"] == 0


async def test_clashing_day_is_skipped_not_double_booked(client, super_token):
    c, h, w = await _setup(client, super_token)
    start = ny_today() + timedelta(days=1)
    first = start
    while first.weekday() not in (0, 2, 4):
        first += timedelta(days=1)
    manual = datetime.combine(first, time(7, 30), tzinfo=NY).astimezone(UTC)
    await new_trip(client, h, w["patient"], w["facility_id"], manual)
    r = await client.post("/recurring-trips", headers=h, json=series_body(w, start, return_pickup_time="11:00:00"))
    skipped = r.json()["generated"]["skipped"]
    assert skipped == [{"date": first.isoformat(), "leg": "outbound", "reason": "patient_overlap"}]
    legs = [t["leg"] for t in await _all_trips(client, h, on_date=first.isoformat())]
    assert sorted(legs) == ["outbound", "return"]                    # the manual one + the series' return trip


async def test_nightly_job_covers_every_company_and_is_repeatable(client, super_token):
    c1, h1, w1 = await _setup(client, super_token, "alpha")
    c2, h2, w2 = await _setup(client, super_token, "bravo")
    start = ny_today() + timedelta(days=2)
    ids = {}
    for key, h, w in (("a", h1, w1), ("b", h2, w2)):
        r = await client.post("/recurring-trips", headers=h, json=series_body(w, start, rrule="FREQ=DAILY;COUNT=5", materialize_now=False))
        ids[key] = r.json()["series"]["id"]
        assert await _all_trips(client, h) == []

    first = await materialize_all()
    assert first["companies"] == 2 and first["series"] == 2 and first["created"] == 10     # platform company ignored
    assert (await materialize_all())["created"] == 0
    for key, h in (("a", h1), ("b", h2)):
        trips = await _all_trips(client, h)
        assert len(trips) == 5 and {t["series_id"] for t in trips} == {ids[key]}             # nothing crossed over


async def test_series_permissions_switch_and_isolation(client, super_token):
    c, h, w = await _setup(client, super_token)
    start = ny_today() + timedelta(days=1)
    disp = await staff_headers(client, c, "dispatcher")
    bill = await staff_headers(client, c, "billing_staff")
    body = series_body(w, start, rrule="FREQ=DAILY;COUNT=2", materialize_now=False)
    assert (await client.post("/recurring-trips", headers=bill, json=body)).status_code == 403
    sid = (await client.post("/recurring-trips", headers=disp, json=body)).json()["series"]["id"]
    assert (await client.get(f"/recurring-trips/{sid}", headers=bill)).status_code == 200
    assert (await client.patch(f"/recurring-trips/{sid}", headers=h, json={"is_active": None})).status_code == 422
    assert (await client.patch(f"/recurring-trips/{sid}", headers=h, json={"end_date": (start - timedelta(days=3)).isoformat()})).status_code == 422
    await client.patch(f"/recurring-trips/{sid}", headers=h, json={"is_active": False})
    assert (await client.post(f"/recurring-trips/{sid}/materialize", headers=h)).json()["created"] == 0   # switched off
    assert (await client.post(f"/recurring-trips/{sid}/materialize", headers=h, params={"until": "2999-01-01"})).status_code == 422
    other = await make_company(client, super_token, "bravo")
    assert (await client.get(f"/recurring-trips/{sid}", headers=other["admin_headers"])).status_code == 404
    assert bearer and new_vehicle and at_local