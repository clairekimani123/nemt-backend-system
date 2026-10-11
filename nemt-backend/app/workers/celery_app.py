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
    },
)