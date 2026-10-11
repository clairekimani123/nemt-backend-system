from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "MediRoutes"
    env: str = "dev"  # dev | test | prod

    database_url: str  # runtime role (RLS enforced)
    database_owner_url: str  # migrations only
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: SecretStr
    jwt_issuer: str = "mediroutes"
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    mfa_token_minutes: int = 5
    password_reset_minutes: int = 30

    field_encryption_keys: str  # comma-separated Fernet keys, newest first
    blind_index_key: SecretStr  # HMAC key for searchable-but-encrypted fields (e.g. Medicaid ID)

    geocoder_backend: str = "none"  # none | fake   (real adapters: see app/core/geo.py)
    storage_dir: str = "./var/storage"
    max_upload_mb: int = 10

    # --- trip engine rules (all tunable per deployment) ---
    default_trip_minutes: int = 45      # used when a trip has no duration estimate
    trip_buffer_minutes: int = 10       # gap a driver/vehicle needs between two trips
    late_grace_minutes: int = 10        # pickup is "late" this long after the scheduled time
    at_risk_hours: int = 2              # unassigned trip this close to pickup = "at risk"
    no_show_wait_minutes: int = 10      # a driver must wait this long before marking a no-show
    late_cancel_hours: int = 2          # cancelling this close to pickup = late cancellation
    recurring_horizon_days: int = 30    # how far ahead recurring series create real trips

    max_failed_logins: int = 5
    lockout_minutes: int = 15

    mail_backend: str = "smtp"  # smtp | memory
    mail_from: str = "no-reply@mediroutes.local"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    frontend_url: str = "http://localhost:3000"
    cors_origins: str = "http://localhost:3000"

    celery_always_eager: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()