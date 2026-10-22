from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery("mediroutes", broker=settings.redis_url, backend=settings.redis_url,
                    include=["app.workers.tasks"])
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_always_eager=settings.celery_always_eager,
    task_acks_late=True,             # re-deliver if a worker dies mid-task
    worker_prefetch_multiplier=1,    # fair dispatch for long tasks
    task_time_limit=120,
    timezone="UTC",
    beat_schedule={
        "materialize-recurring-trips": {
            "task": "app.workers.tasks.materialize_recurring_trips",
            "schedule": crontab(hour=2, minute=0),  # 02:00 UTC every night
        },

        "send-trip-reminders": {
            "task": "app.workers.tasks.send_trip_reminders",
            "schedule": crontab(minute="*/15"),   # every 15 minutes
        },
        "send-daily-driver-manifests": {
            "task": "app.workers.tasks.send_daily_driver_manifests",
            "schedule": crontab(hour=5, minute=0),   # 05:00 UTC
        },

        "send-fleet-expiry-digest": {
            "task": "app.workers.tasks.send_fleet_expiry_digest",
            "schedule": crontab(hour=4, minute=30),   # 04:30 UTC, before the early shift
        },
        "send-daily-summaries": {
            "task": "app.workers.tasks.send_daily_summaries",
            "schedule": crontab(hour=6, minute=30),   # 06:30 UTC daily
        },
        "purge-expired-audit-logs": {
            "task": "app.workers.tasks.purge_expired_audit_logs",
            "schedule": crontab(hour=3, minute=0),    # 03:00 UTC nightly
        },




            
    },
)