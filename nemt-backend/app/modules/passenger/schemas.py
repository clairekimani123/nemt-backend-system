from datetime import date, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.core.address import AddressOut
from app.modules.facilities.models import FacilityKind
from app.modules.patients.models import AssistanceLevel, MobilityType
from app.modules.trips.schemas import PlaceOut, PlaceRef, RoundTripIn
from app.modules.trips.state import CancelReason, Leg, PassengerResponse, TripSource, TripStatus
from app.modules.vehicles.models import VehicleType


class PassengerProfileOut(BaseModel):
    """The passenger's own record. They may see their own DOB; Medicaid ID only as last-4."""

    user_id: UUID
    patient_id: UUID
    email: str
    full_name: str
    first_name: str
    last_name: str
    phone: str | None
    date_of_birth: date
    preferred_language: str
    medicaid_last4: str | None
    mobility: MobilityType
    assistance_level: AssistanceLevel
    requires_escort: bool
    uses_oxygen: bool
    has_service_animal: bool
    companion_name: str | None
    companion_phone: str | None
    companion_relationship: str | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None
    created_at: datetime


class PassengerProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    preferred_language: str | None = Field(default=None, max_length=8)
    mobility: MobilityType | None = None
    requires_escort: bool | None = None
    uses_oxygen: bool | None = None
    has_service_animal: bool | None = None
    companion_name: str | None = Field(default=None, max_length=200)
    companion_phone: str | None = Field(default=None, max_length=32)
    companion_relationship: str | None = Field(default=None, max_length=50)
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        required = {"full_name", "preferred_language", "mobility", "requires_escort", "uses_oxygen",
                    "has_service_animal"}
        bad = sorted(f for f in self.model_fields_set & required if getattr(self, f) is None)
        if bad:
            raise ValueError(f"cannot be null: {', '.join(bad)}")
        return self


class PassengerAddressOut(AddressOut):
    id: UUID
    label: str
    is_primary: bool
    access_notes: str | None


class DestinationOut(BaseModel):
    """A facility as a passenger sees it: where it is, nothing about billing or contacts."""

    id: UUID
    name: str
    kind: FacilityKind
    line1: str
    line2: str | None
    city: str
    state: str
    postal_code: str
    phone: str | None


class RideRequestIn(BaseModel):
    """What a passenger may ask for. Patient, broker, authorisation and duration are not theirs
    to choose: the patient is always the caller, the rest is dispatch's business."""

    pickup: PlaceRef
    dropoff: PlaceRef
    scheduled_pickup_at: AwareDatetime
    appointment_at: AwareDatetime | None = None
    escort_count: int | None = Field(default=None, ge=0, le=3)
    special_instructions: str | None = Field(default=None, max_length=1000)
    round_trip: RoundTripIn | None = None


class PassengerTripOut(BaseModel):
    id: UUID
    trip_ref: str
    status: TripStatus
    leg: Leg
    source: TripSource
    pickup: PlaceOut
    dropoff: PlaceOut
    scheduled_pickup_at: datetime
    appointment_at: datetime | None
    ends_at: datetime
    mobility: MobilityType
    escort_count: int
    special_instructions: str | None
    driver_name: str | None
    vehicle_unit: str | None
    passenger_response: PassengerResponse | None
    return_of_trip_id: UUID | None
    is_late: bool
    en_route_at: datetime | None
    picked_up_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason_code: CancelReason | None
    created_at: datetime


# ------------------------------------------------------------------ assigned driver
class AssignedDriverOut(BaseModel):
    """What a passenger may know about THEIR assigned driver: no licence, address or e-mail."""

    id: UUID
    display_name: str
    has_photo: bool
    photo_updated_at: datetime | None
    phone: str | None  # only while the ride is live (see state.CONTACT_VISIBLE)
    wheelchair_certified: bool


class AssignedVehicleOut(BaseModel):
    unit_number: str
    make: str
    model: str
    year: int
    color: str | None
    plate: str
    plate_state: str
    vehicle_type: VehicleType


class DriverLocationOut(BaseModel):
    latitude: float
    longitude: float
    recorded_at: datetime
    age_seconds: int
    stale: bool                       # older than LOCATION_STALE_SECONDS: may not be current
    straight_line_m: float            # as the crow flies, driver -> YOUR pickup point
    road_distance_m: float | None     # only when a road router (OSRM) is configured
    eta_seconds: int | None           # ditto; never a guess
    eta_source: str | None            # "osrm" | None


class AssignmentOut(BaseModel):
    trip_id: UUID
    trip_ref: str
    status: TripStatus
    passenger_response: PassengerResponse | None
    passenger_responded_at: datetime | None
    assigned_at: datetime | None
    can_respond: bool
    driver: AssignedDriverOut | None
    vehicle: AssignedVehicleOut | None
    pickup_latitude: float
    pickup_longitude: float
    location: DriverLocationOut | None
    location_note: str | None         # why there is no location, in plain words


class AnswerIn(BaseModel):
    driver_id: UUID  # the driver the passenger is answering about (refused if dispatch changed it)
