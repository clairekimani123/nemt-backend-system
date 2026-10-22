from app.core.mail import get_mailer
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, autoretry_for=(OSError,), retry_backoff=True, max_retries=5)
def send_email(self, to: str, subject: str, body: str) -> None:  # type: ignore[no-untyped-def]
    # Rule: never put PHI in email bodies. Notifications phase builds on this task.
    get_mailer().send(to, subject, body)


@celery_app.task
def materialize_recurring_trips() -> dict[str, int]:
    """Nightly: create real trips from every active recurring series."""
    from app.modules.trips.jobs import run_materialize_all

    return run_materialize_all()

@celery_app.task
def send_trip_reminders() -> dict[str, int]:
    """Reminder to patients whose pickup is in the next 2 hours."""
    from app.modules.notifications.jobs import run_trip_reminders
    return run_trip_reminders()


@celery_app.task
def send_daily_driver_manifests() -> dict[str, int]:
    """Each driver gets an email listing today's trips, 90 minutes before their first pickup."""
    from app.modules.notifications.jobs import run_daily_manifests
    return run_daily_manifests()


@celery_app.task
def send_fleet_expiry_digest() -> dict[str, int]:
    """Nightly: email the fleet_manager about vehicles whose docs expire soon.

    Groups by company, sends one email per company that has any expiring items.
    """
    from app.modules.fleet.jobs import run_fleet_expiry_digest
    return run_fleet_expiry_digest()


@celery_app.task
def send_daily_summaries() -> dict[str, int]:
    """Nightly at 06:30 UTC: yesterday's ops summary to each company's staff."""
    from app.modules.reporting.jobs import run_daily_summaries
    return run_daily_summaries()


@celery_app.task
def purge_expired_audit_logs() -> dict[str, int]:
    from app.modules.audit.jobs import run_purge_expired_audit_logs
    return run_purge_expired_audit_logs()