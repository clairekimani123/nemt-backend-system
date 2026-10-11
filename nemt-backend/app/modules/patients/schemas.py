from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.address import AddressIn, AddressOut
from app.core.types import Email
from app.modules.patients.models import AssistanceLevel, MobilityType

MedicaidId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9 -]{6,20}$")]
Short = Annotated[str, StringConstraints(max_length=200)]


def _check_dob(v: date) -> date:
    if v > date.today() or v.year < 1900:
        raise ValueError("date_of_birth out of range")
    return v


class PatientCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: date
    phone: str | None = Field(default=None, max_length=32)
    email: Email | None = None
    preferred_language: str = Field(default="en", max_length=8)
    medicaid_id: MedicaidId | None = None
    mobility: MobilityType = MobilityType.AMBULATORY
    assistance_level: AssistanceLevel = AssistanceLevel.DOOR_TO_DOOR
    requires_escort: bool = False
    uses_oxygen: bool = False
    has_service_animal: bool = False
    weight_lbs: int | None = Field(default=None, ge=20, le=1000)
    companion_name: Short | None = None
    companion_phone: str | None = Field(default=None, max_length=32)
    companion_relationship: str | None = Field(default=None, max_length=50)
    emergency_contact_name: Short | None = None
    emergency_contact_phone: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=4000)
    user_id: UUID | None = None
    home_address: AddressIn | None = None

    _dob = field_validator("date_of_birth")(_check_dob)


class PatientUpdate(BaseModel):
    """Every field optional; only fields actually sent are changed."""

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=32)
    email: Email | None = None
    preferred_language: str | None = Field(default=None, max_length=8)
    medicaid_id: MedicaidId | None = None
    mobility: MobilityType | None = None
    assistance_level: AssistanceLevel | None = None
    requires_escort: bool | None = None
    uses_oxygen: bool | None = None
    has_service_animal: bool | None = None
    weight_lbs: int | None = Field(default=None, ge=20, le=1000)
    companion_name: Short | None = None
    companion_phone: str | None = Field(default=None, max_length=32)
    companion_relationship: str | None = Field(default=None, max_length=50)
    emergency_contact_name: Short | None = None
    emergency_contact_phone: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("date_of_birth")
    @classmethod
    def _dob(cls, v: date | None) -> date | None:
        return _check_dob(v) if v else v

    @model_validator(mode="after")
    def _no_nulls_for_required(self) -> "PatientUpdate":
        required = {"first_name", "last_name", "date_of_birth", "preferred_language", "mobility",
                    "assistance_level", "requires_escort", "uses_oxygen", "has_service_animal"}
        bad = sorted(f for f in self.model_fields_set & required if getattr(self, f) is None)
        if bad:
            raise ValueError(f"cannot be null: {', '.join(bad)}")
        return self


class PatientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    first_name: str
    last_name: str
    phone: str | None
    email: str | None
    preferred_language: str
    medicaid_last4: str | None
    mobility: MobilityType
    assistance_level: AssistanceLevel
    requires_escort: bool
    uses_oxygen: bool
    has_service_animal: bool
    weight_lbs: int | None
    companion_name: str | None
    companion_phone: str | None
    companion_relationship: str | None
    emergency_contact_name: str | None
    emergency_contact_phone: str | None
    user_id: UUID | None
    archived_at: datetime | None
    created_at: datetime
    is_minor: bool = False  # operational need (escort rules) without exposing the DOB
    # --- only for roles holding patients:sensitive; otherwise null ---
    date_of_birth: date | None = None
    medicaid_id: str | None = None
    notes: str | None = None


class PatientAddressCreate(AddressIn):
    label: str = Field(min_length=1, max_length=50)
    is_primary: bool = False
    access_notes: str | None = Field(default=None, max_length=500)


class PatientAddressUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=50)
    is_primary: bool | None = None
    access_notes: str | None = Field(default=None, max_length=500)
    address: AddressIn | None = None


class PatientAddressOut(AddressOut):
    id: UUID
    patient_id: UUID
    label: str
    is_primary: bool
    access_notes: str | None