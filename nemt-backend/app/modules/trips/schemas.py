from datetime import date, datetime, time
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.address import AddressIn
from app.modules.patients.models import MobilityType
from app.modules.trips.recurrence import validate_rrule
from app.modules.trips.state import CancelReason, Leg, TripSource, TripStatus


# ---------------------------------------------------------------- input
class PlaceRef(BaseModel):
    """Where a ride starts or ends. Give EXACTLY ONE of the three."""

    patient_address_id: UUID | None = None   # one of the patient's saved addresses
    facility_id: UUID | None = None          # a hospital / clinic / dialysis centre
    address: AddressIn | None = None         # a one-off address

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if sum(x is not None for x in (self.patient_address_id, self.facility_id, self.address)) != 1:
            raise ValueError("give exactly one of patient_address_id, facility_id, address")
        return self


class RoundTripIn(BaseModel):
    return_pickup_at: AwareDatetime


class TripCreate(BaseModel):
    patient_id: UUID
    pickup: PlaceRef
    dropoff: PlaceRef
    scheduled_pickup_at: AwareDatetime
    appointment_at: AwareDatetime | None = None
    estimated_duration_min: int | None = Field(default=None, ge=10, le=480)
    # the next four default to the patient's profile when omitted
    mobility: MobilityType | None = None
    escort_count: int | None = Field(default=None, ge=0, le=3)
    uses_oxygen: bool | None = None
    has_service_animal: bool | None = None
    special_instructions: str | None = Field(default=None, max_length=1000)
    broker_id: UUID | None = None
    authorization_number: str | None = Field(default=None, max_length=64)
    round_trip: RoundTripIn | None = None

    @model_validator(mode="after")
    def _return_after_pickup(self) -> Self:
        if self.round_trip and self.round_trip.return_pickup_at <= self.scheduled_pickup_at:
            raise ValueError("return_pickup_at must be after scheduled_pickup_at")
        return self


class TripUpdate(BaseModel):
    pickup: PlaceRef | None = None
    dropoff: PlaceRef | None = None
    scheduled_pickup_at: AwareDatetime | None = None
    appointment_at: AwareDatetime | None = None
    estimated_duration_min: int | None = Field(default=None, ge=10, le=480)
    mobility: MobilityType | None = None
    escort_count: int | None = Field(default=None, ge=0, le=3)
    uses_oxygen: bool | None = None
    has_service_animal: bool | None = None
    special_instructions: str | None = Field(default=None, max_length=1000)
    authorization_number: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        required = {"pickup", "dropoff", "scheduled_pickup_at", "estimated_duration_min", "mobility",
                    "escort_count", "uses_oxygen", "has_service_animal"}
        bad = sorted(f for f in self.model_fields_set & required if getattr(self, f) is None)
        if bad:
            raise ValueError(f"cannot be null: {', '.join(bad)}")
        return self


class AssignIn(BaseModel):
    driver_id: UUID
    vehicle_id: UUID


class StatusIn(BaseModel):
    status: TripStatus
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    reason_code: CancelReason | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be sent together")
        if self.status == TripStatus.CANCELLED and self.reason_code is None:
            raise ValueError("reason_code is required when cancelling")
        return self


# ---------------------------------------------------------------- output
class PlaceOut(BaseModel):
    line1: str
    line2: str | None
    city: str
    state: str
    postal_code: str
    latitude: float | None
    longitude: float | None


class PatientBrief(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    phone: str | None


class DriverBrief(BaseModel):
    id: UUID
    full_name: str


class VehicleBrief(BaseModel):
    id: UUID
    unit_number: str


class TripOut(BaseModel):
    id: UUID
    trip_number: int
    trip_ref: str                       # what dispatchers say out loud: T-000123
    status: TripStatus
    leg: Leg
    source: TripSource
    patient: PatientBrief
    driver: DriverBrief | None
    vehicle: VehicleBrief | None
    pickup: PlaceOut
    dropoff: PlaceOut
    scheduled_pickup_at: datetime
    appointment_at: datetime | None
    estimated_duration_min: int
    ends_at: datetime
    mobility: MobilityType
    escort_count: int
    uses_oxygen: bool
    has_service_animal: bool
    special_instructions: str | None
    broker_id: UUID | None
    authorization_number: str | None
    return_of_trip_id: UUID | None
    series_id: UUID | None
    is_late: bool
    at_risk: bool
    en_route_at: datetime | None
    arrived_pickup_at: datetime | None
    picked_up_at: datetime | None
    arrived_dropoff_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    no_show_at: datetime | None
    cancel_reason_code: CancelReason | None
    late_cancel: bool
    created_at: datetime


class IssueOut(BaseModel):
    code: str
    message: str
    trip_id: UUID | None = None


class AssignmentReportOut(BaseModel):
    ok: bool
    errors: list[IssueOut]
    warnings: list[IssueOut]


class TripEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_type: str
    from_status: str | None
    to_status: str | None
    actor_user_id: UUID | None
    occurred_at: datetime
    latitude: float | None
    longitude: float | None
    reason_code: str | None
    meta: dict


class BoardOut(BaseModel):
    on_date: date
    counts: dict[str, int]
    late: int
    lanes: dict[str, list[TripOut]]


class DriverTripOut(BaseModel):
    """What the driver app gets: enough to do the job, no DOB / Medicaid ID / billing data."""

    id: UUID
    trip_ref: str
    status: TripStatus
    patient_first_name: str
    patient_last_name: str
    patient_phone: str | None
    mobility: MobilityType
    escort_count: int
    uses_oxygen: bool
    has_service_animal: bool
    special_instructions: str | None
    pickup: PlaceOut
    dropoff: PlaceOut
    scheduled_pickup_at: datetime
    appointment_at: datetime | None
    vehicle_unit: str | None
    is_late: bool


# ---------------------------------------------------------------- recurring
def _naive(t: time | None) -> time | None:
    if t is not None and t.tzinfo is not None:
        raise ValueError("times are local wall-clock times: send like 07:30:00 with no timezone")
    return t


class RecurringCreate(BaseModel):
    patient_id: UUID
    pickup: PlaceRef
    dropoff: PlaceRef
    rrule: str = Field(max_length=200)
    start_date: date
    end_date: date | None = None
    pickup_time: time
    appointment_time: time | None = None
    return_pickup_time: time | None = None
    estimated_duration_min: int | None = Field(default=None, ge=10, le=480)
    mobility: MobilityType | None = None
    escort_count: int | None = Field(default=None, ge=0, le=3)
    uses_oxygen: bool | None = None
    has_service_animal: bool | None = None
    special_instructions: str | None = Field(default=None, max_length=1000)
    broker_id: UUID | None = None
    authorization_number: str | None = Field(default=None, max_length=64)
    materialize_now: bool = True

    _times = field_validator("pickup_time", "appointment_time", "return_pickup_time")(_naive)

    @field_validator("rrule")
    @classmethod
    def _rule(cls, v: str) -> str:
        return validate_rrule(v)

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        if self.appointment_time and self.appointment_time <= self.pickup_time:
            raise ValueError("appointment_time must be after pickup_time")
        if self.return_pickup_time and self.return_pickup_time <= self.pickup_time:
            raise ValueError("return_pickup_time must be after pickup_time")
        return self


class RecurringUpdate(BaseModel):
    end_date: date | None = None
    is_active: bool | None = None


class RecurringEndIn(BaseModel):
    from_date: date
    reason_code: CancelReason


class SkippedOut(BaseModel):
    date: date
    leg: str
    reason: str


class MaterializeOut(BaseModel):
    created: int
    skipped: list[SkippedOut]


class SeriesOut(BaseModel):
    id: UUID
    patient_id: UUID
    broker_id: UUID | None
    authorization_number: str | None
    rrule: str
    start_date: date
    end_date: date | None
    timezone: str
    pickup_time: time
    appointment_time: time | None
    return_pickup_time: time | None
    estimated_duration_min: int
    mobility: MobilityType
    escort_count: int
    uses_oxygen: bool
    has_service_animal: bool
    special_instructions: str | None
    pickup: PlaceOut
    dropoff: PlaceOut
    is_active: bool
    materialized_through: date | None
    created_at: datetime


class SeriesCreatedOut(BaseModel):
    series: SeriesOut
    generated: MaterializeOut