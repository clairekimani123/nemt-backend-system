"""Nightly operational summary.

At 06:30 UTC, send one email per tenant with yesterday's numbers. The content mirrors
what the /reports endpoints return, so a change to the aggregation logic is a change
in one place (the service functions), not two.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.core.db import engine, open_tenant_session
from app.core.permissions import Role
from app.modules.notifications import service as notifier
from app.modules.notifications.models import Channel
from app.modules.reporting import service as reports
from app.modules.tenants.models import Company
from app.modules.users.models import User

log = logging.getLogger(__name__)

# Roles that should receive the daily ops summary
RECIPIENT_ROLES = {Role.COMPANY_ADMIN, Role.DISPATCHER, Role.BILLING_STAFF, Role.FLEET_MANAGER}


async def _recipients_for(db, company_id: str) -> list[tuple[str, str]]:
    """Return [(email, full_name)] for everyone in the company who should receive the summary."""
    users = (await db.scalars(select(User).where(
        User.company_id == company_id,
        User.is_active.is_(True),
        User.role.in_(RECIPIENT_ROLES),
    ).order_by(User.email))).all()
    return [(u.email, u.full_name) for u in users]


async def _yesterday_for(db, company_id: str) -> date:
    """Yesterday in the company's local timezone."""
    from app.modules.trips.service import company_tz
    tz = await company_tz(db, company_id)
    return (datetime.now(tz) - timedelta(days=1)).date()


def _format_currency(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _format_bp(bp: int) -> str:
    """Basis points to a percent string. 5000 → '50.0%'."""
    return f"{bp / 100:.1f}%"


def _render_summary(
    *,
    company_name: str,
    day: date,
    volume,
    on_time,
    cancellations,
    no_shows,
    revenue,
    top_drivers,
) -> tuple[str, str]:
    """Return (subject, body) as plain text."""
    subject = f"{company_name}: daily summary for {day.strftime('%A %B %d, %Y')}"

    lines = [
        f"Daily summary — {day.strftime('%A %B %d, %Y')}",
        f"Company: {company_name}",
        "",
        "─── Trips ───",
        f"  Total trips:        {volume.total_trips}",
        f"  Completed:          {sum(b.completed for b in volume.by_day)}",
        f"  Cancelled:          {sum(b.cancelled for b in volume.by_day)}",
        f"  No-shows:           {sum(b.no_show for b in volume.by_day)}",
        "",
        "─── On-time performance ───",
        f"  Pickups measured:   {on_time.overall.total_pickups}",
        f"  On time:            {on_time.overall.on_time_count}",
        f"  Late:               {on_time.overall.late_count}",
        f"  On-time rate:       {_format_bp(on_time.overall.on_time_rate_bp)}",
    ]
    if on_time.overall.avg_late_minutes is not None:
        lines.append(f"  Avg late (mins):    {on_time.overall.avg_late_minutes:.1f}")
    lines += [
        "",
        "─── Cancellations ───",
        f"  Total cancelled:    {cancellations.total}",
        f"  Late cancellations: {sum(r.late_cancellations for r in cancellations.by_reason)}",
        f"  Late rate:          {_format_bp(cancellations.late_rate_bp)}",
    ]
    if cancellations.by_reason:
        lines.append("  By reason:")
        for r in cancellations.by_reason:
            lines.append(f"    {r.reason_code or '(unspecified)'}: {r.count}")
    lines += [
        "",
        "─── No-shows ───",
        f"  Total:              {no_shows.total}",
    ]
    lines += [
        "",
        "─── Revenue (claims service date) ───",
        f"  Trips billed:       {revenue.trips_completed}",
        f"  Billed:             {_format_currency(revenue.billed_cents)}",
        f"  Collected:          {_format_currency(revenue.paid_cents)}",
        f"  Revenue / trip:     {_format_currency(revenue.revenue_per_trip_cents)}",
        f"  Paid rate:          {_format_bp(revenue.paid_rate_bp)}",
    ]
    if top_drivers.rows:
        lines += [
            "",
            "─── Top drivers ───",
        ]
        for r in top_drivers.rows[:5]:
            lines.append(
                f"  {r.rank}. {r.driver_name}: {r.completed_trips} trips, "
                f"{_format_bp(r.on_time_rate_bp)} on-time"
            )
    lines += [
        "",
        "--",
        "MediRoutes daily ops summary. Do not reply to this email.",
    ]
    return subject, "\n".join(lines)


async def send_daily_summaries_once() -> dict[str, int]:
    """Send yesterday's summary to every active company that had trips.

    Returns {"companies_sent": n, "emails_sent": n, "skipped_no_trips": n}.
    """
    stats = {"companies_sent": 0, "emails_sent": 0, "skipped_no_trips": 0}

    # Enumerate companies without a tenant scope (needs bypass)
    async with open_tenant_session(None, bypass=True) as db:
        companies = (await db.scalars(select(Company).where(
            Company.is_active.is_(True),
            Company.is_platform.is_(False),
        ))).all()

    for c in companies:
        async with open_tenant_session(c.id) as db:
            day = await _yesterday_for(db, c.id)
            try:
                volume = await reports.volume_report(db, c.id, day, day)
            except Exception as exc:
                log.exception("summary: volume failed for %s: %s", c.id, exc)
                continue

            if volume.total_trips == 0:
                stats["skipped_no_trips"] += 1
                continue

            try:
                on_time = await reports.on_time_report(db, c.id, day, day)
                cancellations = await reports.cancellation_report(db, c.id, day, day)
                no_shows = await reports.no_show_report(db, c.id, day, day)
                revenue = await reports.revenue_productivity(db, c.id, day, day)
                top_drivers = await reports.leaderboard(db, c.id, day, day, limit=5)
            except Exception as exc:
                log.exception("summary: metrics failed for %s: %s", c.id, exc)
                continue

            recipients = await _recipients_for(db, c.id)
            if not recipients:
                continue

            subject, body = _render_summary(
                company_name=c.name,
                day=day,
                volume=volume,
                on_time=on_time,
                cancellations=cancellations,
                no_shows=no_shows,
                revenue=revenue,
                top_drivers=top_drivers,
            )

            for email, _name in recipients:
                await notifier.notify(
                    db, company_id=c.id, event="ops.daily_summary",
                    channel=Channel.EMAIL, to=email, recipient_user_id=None,
                    context={
                        "company_name": c.name,
                        "day": day.isoformat(),
                        "subject": subject,
                        "body": body,
                    },
                )
                stats["emails_sent"] += 1
            await db.commit()
            stats["companies_sent"] += 1

    return stats


def run_daily_summaries() -> dict[str, int]:
    """Celery entrypoint."""

    async def _go():
        try:
            return await send_daily_summaries_once()
        finally:
            await engine.dispose()

    return asyncio.run(_go())