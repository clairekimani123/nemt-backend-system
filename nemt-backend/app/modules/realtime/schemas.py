"""WebSocket message schemas.

Every message is a Pydantic model with a `type` discriminator. Unknown types are ignored,
never errored, so the protocol can grow without breaking old clients.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.permissions import Role
from app.modules.trips.state import TripStatus


# ------------------------------------------------------------------ client → server
class AuthIn(BaseModel):
    type: Literal["auth"]
    token: str


class PingIn(BaseModel):
    type: Literal["ping"]


class LocationIn(BaseModel):
    type: Literal["location"]
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    trip_id: UUID | None = None
    speed_mps: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    accuracy_m: float | None = Field(default=None, ge=0)


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