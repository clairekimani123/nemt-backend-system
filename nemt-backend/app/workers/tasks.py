from app.core.mail import get_mailer
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, autoretry_for=(OSError,), retry_backoff=True, max_retries=5)
def send_email(self, to: str, subject: str, body: str) -> None:  # type: ignore[no-untyped-def]
    # Rule: never put PHI in email bodies. Notifications phase builds on this task.
    get_mailer().send(to, subject, body)