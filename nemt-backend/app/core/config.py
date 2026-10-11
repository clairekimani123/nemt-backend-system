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