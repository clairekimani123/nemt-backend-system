"""Request and response shapes for the routing API."""

from datetime import date, datetime, time
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.modules.patients.models import MobilityType
from app.modules.routing.models import PlanStatus, RouteStatus
from app.modules.trips.schemas import DriverBrief, PlaceOut, VehicleBrief
from app.modules.trips.state import TripStatus


class ResourceRef(BaseModel):
    driver_id: UUID
    vehicle_id: UUID


class DepotIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class PlanRequest(BaseModel):
    on_date: date
    resources: list[ResourceRef] | None = Field(default=None, max_length=40)
    trip_ids: list[UUID] | None = Field(default=None, max_length=120)
    include_assigned: bool = False
    depot: DepotIn | None = None
    shift_start: time | None = None
    shift_end: time | None = None
    time_limit_s: int | None = Field(default=None, ge=1, le=30)

    @model_validator(mode="after")
    def _shift(self) -> "PlanRequest":
        if (self.shift_start or self.shift_end) and not (self.shift_start and self.shift_end):
            raise ValueError("send shift_start and shift_end together")
        if self.shift_start and self.shift_end and self.shift_end <= self.shift_start:
            raise ValueError("shift_end must be after shift_start")
        return self


class EstimateIn(BaseModel):
    apply: bool = False


class LoadOut(BaseModel):
    seats: int
    wheelchair: int
    stretcher: int


class StopOut(BaseModel):
    sequence: int
    kind: str
    trip_id: UUID
    trip_ref: str
    trip_status: TripStatus
    patient_first_name: str
    patient_last_name: str
    mobility: MobilityType
    place: PlaceOut
    planned_arrival_at: datetime
    planned_departure_at: datetime
    wait_minutes: int
    booked_time: datetime | None
    delta_minutes: int | None
    on_board_after: LoadOut


class RouteOut(BaseModel):
    id: UUID
    plan_id: UUID
    route_date: date
    status: RouteStatus
    driver: DriverBrief
    vehicle: VehicleBrief
    planned_start_at: datetime
    planned_end_at: datetime
    drive_minutes: int
    distance_km: float
    stops: list[StopOut]


class UnassignedOut(BaseModel):
    trip_id: UUID
    trip_ref: str
    reasons: list[str]
    was_assigned: bool


class SkippedResourceOut(BaseModel):
    driver_id: UUID
    vehicle_id: UUID
    reason: str


class PlanOut(BaseModel):
    id: UUID
    route_date: date
    status: PlanStatus
    metrics: dict
    routes: list[RouteOut]
    unassigned: list[UnassignedOut]
    skipped_resources: list[SkippedResourceOut]
    created_at: datetime


class ApplyOut(BaseModel):
    plan_id: UUID
    routes_published: int
    trips_assigned: int
    trips_unassigned: int


class AppointmentOut(BaseModel):
    appointment_at: datetime
    latest_pickup_at: datetime
    slack_minutes: int
    ok: bool


class EstimateOut(BaseModel):
    drive_minutes: int
    distance_km: float
    dwell_minutes: int
    estimated_duration_min: int
    traffic_factor: float
    source: str
    appointment: AppointmentOut | None
    applied: bool


class GeometryOut(BaseModel):
    type: str = "LineString"
    coordinates: list[list[float]]
    distance_km: float
    duration_min: float
    source: str


class DriverStopOut(BaseModel):
    sequence: int
    kind: str
    trip_id: UUID
    trip_ref: str
    trip_status: TripStatus
    patient_first_name: str
    patient_last_name: str
    patient_phone: str | None
    mobility: MobilityType
    escort_count: int
    uses_oxygen: bool
    has_service_animal: bool
    special_instructions: str | None
    place: PlaceOut
    planned_arrival_at: datetime
    booked_time: datetime | None


class DriverRouteOut(BaseModel):
    route_id: UUID
    route_date: date
    vehicle_unit: str
    stops: list[DriverStopOut]