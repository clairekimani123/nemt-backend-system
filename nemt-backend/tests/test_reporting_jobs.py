"""The nightly ops summary job: format, filtering, tenant scoping."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.modules.reporting.jobs import _render_summary, _format_currency, _format_bp
from tests.helpers import (
    PW, assign, at, bearer, login, make_company, new_driver, new_facility, new_patient,
    new_trip, new_vehicle, staff_headers, world,
)


# ------------------------------------------------------------------ pure formatting
def test_format_currency():
    assert _format_currency(0) == "$0.00"
    assert _format_currency(1234) == "$12.34"
    assert _format_currency(123456789) == "$1,234,567.89"


def test_format_bp():
    assert _format_bp(0) == "0.0%"
    assert _format_bp(5000) == "50.0%"
    assert _format_bp(10000) == "100.0%"
    assert _format_bp(4733) == "47.3%"


# ------------------------------------------------------------------ render output
def test_render_summary_includes_all_sections():
    from uuid import uuid4
    from app.modules.reporting.schemas import (
        BreakdownRow, CancellationReport, CancellationRow, LeaderboardReport,
        LeaderboardRow, NoShowReport, OnTimeReport, OnTimeRow, PeriodOut,
        RevenueProductivityOut, VolumeBucket, VolumeReport,
    )

    period = PeriodOut(from_date=date(2026, 10, 15), to_date=date(2026, 10, 15))
    volume = VolumeReport(
        period=period, total_trips=42,
        by_day=[VolumeBucket(bucket=period.from_date, total=42, completed=38,
                             cancelled=3, no_show=1)],
        by_mobility=[BreakdownRow(key="ambulatory", label="ambulatory", trip_count=42,
                                  completed_count=38, cancelled_count=3, no_show_count=1)],
        by_source=[BreakdownRow(key="dispatcher", label="dispatcher", trip_count=42,
                                completed_count=38, cancelled_count=3, no_show_count=1)],
    )
    on_time = OnTimeReport(
        period=period,
        overall=OnTimeRow(total_pickups=38, on_time_count=36, late_count=2,
                          avg_late_minutes=6.5, on_time_rate_bp=9473),
        by_driver=[],
    )
    cancellations = CancellationReport(
        period=period, total=3,
        by_reason=[CancellationRow(reason_code="patient_cancelled", count=3, late_cancellations=1)],
        late_rate_bp=3333,
    )
    no_shows = NoShowReport(period=period, total=1, by_driver=[])
    revenue = RevenueProductivityOut(
        period=period, trips_completed=38, total_distance_km=412.5,
        billed_cents=285000, paid_cents=210000,
        revenue_per_trip_cents=7500, revenue_per_km_cents=691,
        paid_rate_bp=7368,
    )
    top_drivers = LeaderboardReport(period=period, rows=[
        LeaderboardRow(rank=1, driver_id=uuid4(), driver_name="Dan Driver",
                       completed_trips=25, on_time_rate_bp=9600,
                       cancelled_trips=0, no_show_trips=0, distance_km=200.5),
    ])

    subject, body = _render_summary(
        company_name="Alpha Transport",
        day=date(2026, 10, 15),
        volume=volume, on_time=on_time, cancellations=cancellations,
        no_shows=no_shows, revenue=revenue, top_drivers=top_drivers,
    )

    assert "Alpha Transport" in subject
    assert "Thursday October 15, 2026" in subject
    assert "42" in body
    assert "94.7%" in body
    assert "$2,850.00" in body
    assert "$2,100.00" in body
    assert "73.7%" in body
    assert "Dan Driver" in body
    assert "patient_cancelled" in body

# ------------------------------------------------------------------ integration
async def test_daily_summaries_job_sends_and_respects_tenant_isolation(client, super_token):
    from datetime import UTC, timedelta
    import uuid as _uuid
    from app.modules.reporting.jobs import send_daily_summaries_once
    from tests.helpers import at, sql

    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]

    # Seed the template this test needs. Do not rely on the Phase 9.2 seed script
    # having run against nemt_test — tests must be self-contained.
    r = await client.post("/notifications/templates", headers=h, json={
        "event": "ops.daily_summary", "channel": "email",
        "subject": "{{subject}}", "body": "{{body}}",
    })
    assert r.status_code == 201, r.text

    w = await world(client, c)

    when = at(0, 20)
    t = await new_trip(client, h, w["patient"], w["facility_id"], when)
    await assign(client, h, t["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])
    for s in ("en_route_to_pickup", "arrived_at_pickup", "passenger_on_board",
              "en_route_to_dropoff", "arrived_at_dropoff", "completed"):
        r = await client.post(f"/driver/trips/{t['id']}/status", headers=drv,
                              json={"status": s, "latitude": 38.9, "longitude": -77.0})
        assert r.status_code == 200, r.text

    yesterday_dt = datetime.now(UTC) - timedelta(days=1)
    await sql(
        "UPDATE trips SET scheduled_pickup_at = :start, ends_at = :end WHERE id = :i",
        start=yesterday_dt,
        end=yesterday_dt + timedelta(minutes=45),
        i=_uuid.UUID(t["id"]),
    )

    result = await send_daily_summaries_once()
    assert result["companies_sent"] >= 1
    assert result["emails_sent"] >= 1

    from sqlalchemy import text
    from app.core.db import open_tenant_session
    async with open_tenant_session(c["company"]["id"]) as db:
        rows = (await db.execute(text(
            "SELECT status, event, error FROM notification_logs WHERE event='ops.daily_summary'"
        ))).all()
    assert len(rows) >= 1
    # assert each row individually so the failure message tells you why
    for status, event, error in rows:
        assert status == "sent", f"expected sent, got {status} ({error})"
async def test_daily_summaries_job_skips_empty_companies(client, super_token):
    from app.modules.reporting.jobs import send_daily_summaries_once
    c = await make_company(client, super_token, "emptyco")   # slug without underscore
    result = await send_daily_summaries_once()
    assert result["skipped_no_trips"] >= 1