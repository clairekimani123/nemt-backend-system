"""Response shapes for operational reports.

Everything is a request/response DTO — no models, no tables. The data comes from
`trips`, `claims`, `trip_events`, and `driver_positions`, aggregated on the fly.
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel

from app.modules.patients.models import MobilityType


# ------------------------------------------------------------------ shared
class PeriodOut(BaseModel):
    from_date: date
    to_date: date


class BreakdownRow(BaseModel):
    """One row in a `dimension -> metrics` breakdown."""

    key: str
    label: str
    trip_count: int
    completed_count: int
    cancelled_count: int
    no_show_count: int


# ------------------------------------------------------------------ on-time
class OnTimeRow(BaseModel):
    """On-time metrics for one driver or one company."""

    driver_id: UUID | None = None
    driver_name: str | None = None
    total_pickups: int
    on_time_count: int
    late_count: int
    avg_late_minutes: float | None
    on_time_rate_bp: int                    # basis points, 0-10000


class OnTimeReport(BaseModel):
    period: PeriodOut
    overall: OnTimeRow
    by_driver: list[OnTimeRow]              # sorted by on_time_rate_bp asc (worst first)


# ------------------------------------------------------------------ cancellations / no-shows
class CancellationRow(BaseModel):
    reason_code: str | None
    count: int
    late_cancellations: int                 # cancelled within the "late" window


class CancellationReport(BaseModel):
    period: PeriodOut
    total: int
    by_reason: list[CancellationRow]
    late_rate_bp: int                       # late cancellations / total, in bp


class NoShowReport(BaseModel):
    period: PeriodOut
    total: int
    by_driver: list[BreakdownRow]           # sorted by count desc


# ------------------------------------------------------------------ volume
class VolumeBucket(BaseModel):
    bucket: date                            # the day (for daily) or the week start
    total: int
    completed: int
    cancelled: int
    no_show: int


class VolumeReport(BaseModel):
    period: PeriodOut
    total_trips: int
    by_day: list[VolumeBucket]
    by_mobility: list[BreakdownRow]
    by_source: list[BreakdownRow]


# ------------------------------------------------------------------ utilization
class UtilizationRow(BaseModel):
    driver_id: UUID
    driver_name: str
    trips_driven: int
    active_days: int
    scheduled_hours: float                  # sum of assigned-trip durations
    driving_minutes: int                    # actual from trip_events (en_route → completed)
    idle_minutes: int                       # active span − driving
    utilization_bp: int                     # driving / active, in bp


class DriverUtilizationReport(BaseModel):
    period: PeriodOut
    rows: list[UtilizationRow]


class VehicleUtilizationRow(BaseModel):
    vehicle_id: UUID
    unit_number: str
    trips_driven: int
    distance_km: float
    maintenance_cost_cents: int


class VehicleUtilizationReport(BaseModel):
    period: PeriodOut
    rows: list[VehicleUtilizationRow]


# ------------------------------------------------------------------ revenue productivity
class RevenueProductivityOut(BaseModel):
    period: PeriodOut
    trips_completed: int
    total_distance_km: float
    billed_cents: int
    paid_cents: int
    revenue_per_trip_cents: int
    revenue_per_km_cents: int
    paid_rate_bp: int                       # paid / billed, in bp


# ------------------------------------------------------------------ driver leaderboard
class LeaderboardRow(BaseModel):
    rank: int
    driver_id: UUID
    driver_name: str
    completed_trips: int
    on_time_rate_bp: int
    cancelled_trips: int
    no_show_trips: int
    distance_km: float


class LeaderboardReport(BaseModel):
    period: PeriodOut
    rows: list[LeaderboardRow]