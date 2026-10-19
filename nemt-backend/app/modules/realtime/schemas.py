"""WebSocket message schemas.

Every message is a Pydantic model with a `type` discriminator. Unknown types are ignored,
never errored, so the protocol can grow without breaking old clients.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.core.permissions import Role
from app.modules.trips.state import TripStatus


# ------------------------------------------------------------------ client → server
class AuthIn(BaseModel):
    type: Literal["auth"]
    token: str


class PingIn(BaseModel):
    type: Literal["ping"]


class LocationPing(BaseModel):
    """One GPS fix from the driver's device (REST: POST /driver/location)."""

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    trip_id: UUID | None = None  # must be one of the caller's own live trips
    speed_mps: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    accuracy_m: float | None = Field(default=None, ge=0)
    recorded_at: AwareDatetime | None = None  # device time of the fix; server time when omitted


class LocationIn(LocationPing):
    type: Literal["location"]


# ------------------------------------------------------------------ server → client
class AuthOk(BaseModel):
    type: Literal["auth_ok"] = "auth_ok"
    role: Role
    company_id: UUID


class AuthErr(BaseModel):
    type: Literal["auth_err"] = "auth_err"
    detail: str


class Pong(BaseModel):
    type: Literal["pong"] = "pong"


class TripStatusEvent(BaseModel):
    type: Literal["trip.status"] = "trip.status"
    trip_id: UUID
    status: TripStatus
    driver_id: UUID | None
    vehicle_id: UUID | None
    at: datetime


class TripAssignedEvent(BaseModel):
    type: Literal["trip.assigned"] = "trip.assigned"
    trip_id: UUID
    driver_id: UUID
    vehicle_id: UUID
    at: datetime


class DriverPositionEvent(BaseModel):
    type: Literal["driver.position"] = "driver.position"
    driver_id: UUID
    lat: float
    lng: float
    trip_id: UUID | None
    at: datetime

class TripChangedEvent(BaseModel):
    """Sent to ONE user (e.g. the passenger) so their screen refetches the trip. IDs only."""

    type: Literal["trip.changed"] = "trip.changed"
    trip_id: UUID
    change: str  # assigned | unassigned | ...
    at: datetime


class AssignmentAnsweredEvent(BaseModel):
    """The passenger accepted or declined the assigned driver (to dispatch + that driver)."""

    type: Literal["trip.assignment_answered"] = "trip.assignment_answered"
    trip_id: UUID
    driver_id: UUID
    accepted: bool
    at: datetime


class TripCreatedEvent(BaseModel):
    """A new trip exists (booked by dispatch or requested from the passenger portal)."""

    type: Literal["trip.created"] = "trip.created"
    trip_id: UUID
    source: str
    at: datetime


class PatientCreatedEvent(BaseModel):
    """A passenger registered (or dispatch added a patient). IDs only: no PHI on the bus."""

    type: Literal["patient.created"] = "patient.created"
    patient_id: UUID
    self_registered: bool
    at: datetime
