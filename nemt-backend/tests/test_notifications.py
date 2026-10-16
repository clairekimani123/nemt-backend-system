"""Notification engine: template CRUD, rendering, log rows, and the trip hooks."""

from datetime import UTC, datetime

from sqlalchemy import text

from app.core.db import open_tenant_session
from app.modules.notifications.channels import RenderedMessage, get_channel
from app.modules.notifications.models import Channel
from app.modules.notifications.service import notify, render
from tests.helpers import (
    PW, assign, at, bearer, login, make_company, new_driver, new_facility, new_patient,
    new_trip, new_vehicle, world,
)


# ------------------------------------------------------------------ rendering


def test_render_replaces_placeholders_and_swallows_missing_keys():
    assert render("Hi {{name}}!", {"name": "Ada"}) == "Hi Ada!"
    assert render("Hi {{name}}!", {}) == "Hi !"                       # missing key → empty
    assert render("No placeholders", {"x": 1}) == "No placeholders"
    assert render("{{a}} {{b}}", {"a": "1", "b": "2"}) == "1 2"


# ------------------------------------------------------------------ template CRUD


async def test_template_crud_and_tenant_scoping(client, super_token):
    a = await make_company(client, super_token, "alpha")
    b = await make_company(client, super_token, "bravo")
    r = await client.post("/notifications/templates", headers=a["admin_headers"], json={
        "event": "trip.assigned.driver", "channel": "email",
        "subject": "New", "body": "Hello {{driver_name}}",
    })
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["event"] == "trip.assigned.driver" and t["channel"] == "email"

    # bravo cannot see it
    listing = (await client.get("/notifications/templates", headers=b["admin_headers"])).json()
    assert listing == []

    # duplicate (event, channel, language) is rejected
    r2 = await client.post("/notifications/templates", headers=a["admin_headers"], json={
        "event": "trip.assigned.driver", "channel": "email", "body": "Duplicate"})
    assert r2.status_code == 409

    # patch works
    r3 = await client.patch(f"/notifications/templates/{t['id']}", headers=a["admin_headers"],
                            json={"subject": "Updated"})
    assert r3.status_code == 200 and r3.json()["subject"] == "Updated"


# ------------------------------------------------------------------ engine


async def test_notify_writes_sent_and_skipped_logs(client, super_token):
    a = await make_company(client, super_token, "alpha")
    cid = a["company"]["id"]

    # seed a template
    await client.post("/notifications/templates", headers=a["admin_headers"], json={
        "event": "test.event", "channel": "email",
        "subject": "Hi {{name}}", "body": "Body for {{name}}",
    })

    async with open_tenant_session(cid) as db:
        # sent
        await notify(db, company_id=cid, event="test.event", channel=Channel.EMAIL,
                     to="someone@example.com", context={"name": "Ada"})
        # skipped (no template)
        await notify(db, company_id=cid, event="missing.event", channel=Channel.EMAIL,
                     to="someone@example.com", context={})
        await db.commit()

    async with open_tenant_session(cid) as db:
        rows = (await db.execute(text(
            "SELECT event, status, subject, body_preview FROM notification_logs ORDER BY id"
        ))).all()
    assert len(rows) == 2
    assert rows[0].event == "test.event" and rows[0].status == "sent"
    assert rows[0].subject == "Hi Ada" and rows[0].body_preview == "Body for Ada"
    assert rows[1].event == "missing.event" and rows[1].status == "skipped"


# ------------------------------------------------------------------ integration with trips


async def test_assign_trip_emails_the_driver(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    # seed the driver template
    await client.post("/notifications/templates", headers=h, json={
        "event": "trip.assigned.driver", "channel": "email",
        "subject": "New trip", "body": "Hi {{driver_name}}, trip {{trip_ref}} at {{pickup_at}}",
    })
    w = await world(client, c)
    trip = await new_trip(client, h, w["patient"], w["facility_id"], at(3))
    assert (await assign(client, h, trip["id"], w["driver"]["id"], w["vehicle_id"])).status_code == 200

    async with open_tenant_session(c["company"]["id"]) as db:
        rows = (await db.execute(text(
            "SELECT event, status, recipient_address FROM notification_logs WHERE event='trip.assigned.driver'"
        ))).all()
    assert len(rows) == 1
    assert rows[0].status == "sent"
    assert rows[0].recipient_address == w["driver"]["email"]


async def test_en_route_emails_the_patient(client, super_token):
    c = await make_company(client, super_token, "alpha")
    h = c["admin_headers"]
    await client.post("/notifications/templates", headers=h, json={
        "event": "trip.en_route.patient", "channel": "email",
        "subject": "On the way", "body": "Hi {{patient_first_name}}",
    })
    w = await world(client, c)
    trip = await new_trip(client, h, w["patient"], w["facility_id"], at(3))
    await assign(client, h, trip["id"], w["driver"]["id"], w["vehicle_id"])
    drv = bearer((await login(client, "alpha", w["driver"]["email"]))["access_token"])

    r = await client.post(f"/driver/trips/{trip['id']}/status", headers=drv,
                          json={"status": "en_route_to_pickup", "latitude": 38.9, "longitude": -77.0})
    assert r.status_code == 200, r.text

    async with open_tenant_session(c["company"]["id"]) as db:
        rows = (await db.execute(text(
            "SELECT status, recipient_address FROM notification_logs WHERE event='trip.en_route.patient'"
        ))).all()
    assert len(rows) == 1
    assert rows[0].status == "sent"
    assert rows[0].recipient_address.startswith("pat") or "@" in rows[0].recipient_address


# ------------------------------------------------------------------ channel sanity


def test_email_channel_uses_celery_task(monkeypatch):
    """The email channel should not do SMTP inline — it should enqueue a task."""
    sent: list[tuple] = []
    monkeypatch.setattr("app.workers.tasks.send_email.delay",
                        lambda *args: sent.append(args))
    get_channel("email").send(RenderedMessage(to="x@y.z", subject="s", body="b"))
    assert sent and sent[0][0] == "x@y.z"