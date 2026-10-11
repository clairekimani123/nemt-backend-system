from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.address import AddressIn, AddressOut
from app.core.types import Email
from app.modules.facilities.models import FacilityKind


class FacilityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: FacilityKind
    address: AddressIn
    phone: str | None = Field(default=None, max_length=32)
    email: Email | None = None
    contact_name: str | None = Field(default=None, max_length=200)
    transportation_requirements: str | None = Field(default=None, max_length=4000)
    billing_contact_email: Email | None = None
    billing_notes: str | None = Field(default=None, max_length=4000)


class FacilityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: FacilityKind | None = None
    address: AddressIn | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: Email | None = None
    contact_name: str | None = Field(default=None, max_length=200)
    transportation_requirements: str | None = Field(default=None, max_length=4000)
    billing_contact_email: Email | None = None
    billing_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("name", "kind", "address")
    @classmethod
    def _not_null(cls, v):  # type: ignore[no-untyped-def]
        if v is None:
            raise ValueError("cannot be null")
        return v


class FacilityOut(AddressOut):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    kind: FacilityKind
    phone: str | None
    email: str | None
    contact_name: str | None
    transportation_requirements: str | None
    billing_contact_email: str | None
    billing_notes: str | None
    archived_at: datetime | None
    created_at: datetime


class FacilityNearbyOut(FacilityOut):
    distance_m: float


class BrokerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{2,32}$")
    phone: str | None = Field(default=None, max_length=32)
    email: Email | None = None
    contact_name: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("code")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class BrokerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: Email | None = None
    contact_name: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=4000)


class BrokerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    code: str
    phone: str | None
    email: str | None
    contact_name: str | None
    website: str | None
    notes: str | None
    archived_at: datetime | None
    created_at: datetime