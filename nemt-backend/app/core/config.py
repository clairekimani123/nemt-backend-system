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

        # --- rate limiting ---
        # --- rate limiting ---
    rate_limit_login_ip_per_min: int = 20
    rate_limit_login_email_per_min: int = 5
    rate_limit_register_per_hour: int = 5
    rate_limit_forgot_per_hour: int = 3
    rate_limit_mfa_ip_per_min: int = 10
    rate_limit_in_tests: bool = False       # set True in the rate-limit test only
        # --- retention ---
    audit_retention_days: int = 365 * 6        # 6 years per HIPAA
    phi_retention_days: int = 365 * 7          # 7 years per typical state law

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

    # --- routing engine (Phase 4) ---
    routing_backend: str = "haversine"  # haversine | osrm
    osrm_url: str = "http://localhost:5000"  # only used when routing_backend = "osrm"
    osrm_profile: str = "driving"
    osrm_timeout_s: int = 5
    osrm_max_table_size: int = 100  # OSRM's --max-table-size (rows x cols per request)

    # Haversine fallback (used when routing_backend = "haversine", and in tests)
    haversine_speed_kmh: float = 40.0    # average door-to-door speed for a mixed city fleet
    haversine_circuity: float = 1.35     # straight-line distance x this = realistic road distance

    # Traffic model (a simple rush-hour multiplier; off by default)
    traffic_rush_factor: float = 1.0     # 1.0 = no traffic effect. Try 1.5 to model rush hour
    traffic_morning_start_h: int = 7
    traffic_morning_end_h: int = 9       # [start, end)
    traffic_morning_end_m: int = 30      # 09:30 sharp; so morning window is [07:00, 09:30)
    traffic_evening_start_h: int = 15
    traffic_evening_start_m: int = 30
    traffic_evening_end_h: int = 18
    traffic_evening_end_m: int = 30

    # Service (dwell) times by mobility type — minutes spent loading and unloading
    dwell_ambulatory_min: int = 2
    dwell_walker_min: int = 5
    dwell_wheelchair_min: int = 8
    dwell_stretcher_min: int = 15

    # Pickup appointment windows
    pickup_early_min: int = 15           # may arrive this early (solver) — negative offset
    pickup_late_min: int = 10            # may arrive this late (solver) — positive offset
    appointment_buffer_min: int = 10     # target arrival margin before appointment time
    max_ride_factor: float = 2.0         # max ride time = factor x direct + extra
    max_ride_extra_min: int = 15

    # Solver
    solver_seconds: int = 3              # time budget for one plan
    vehicle_fixed_cost: int = 0          # penalty for using an extra vehicle (in solver "minutes")

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