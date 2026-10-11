from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.vehicles.models import Equipment, VehicleStatus, VehicleType

_VIN = r"^[A-HJ-NPR-Za-hj-npr-z0-9]{17}$"  # VINs never contain I, O or Q


class VehicleBase(BaseModel):
    unit_number: str = Field(min_length=1, max_length=32)
    vin: str = Field(pattern=_VIN)
    plate: str = Field(min_length=1, max_length=16)
    plate_state: str = Field(pattern=r"^[A-Za-z]{2}$")
    make: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=50)
    year: int = Field(ge=1980, le=date.today().year + 1)
    vehicle_type: VehicleType
    seat_capacity: int = Field(ge=0, le=60)
    wheelchair_capacity: int = Field(default=0, ge=0, le=10)
    stretcher_capacity: int = Field(default=0, ge=0, le=4)
    equipment: Equipment = Equipment.NONE
    odometer_miles: int = Field(default=0, ge=0)
    registration_expires_on: date | None = None
    insurance_policy_number: str | None = Field(default=None, max_length=64)
    insurance_expires_on: date | None = None
    default_driver_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("vin", "plate", "plate_state")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class VehicleCreate(VehicleBase):
    status: VehicleStatus = VehicleStatus.ACTIVE

    @model_validator(mode="after")
    def _accessibility(self) -> "VehicleCreate":
        if (self.wheelchair_capacity or self.stretcher_capacity) and self.equipment == Equipment.NONE:
            raise ValueError("vehicles with wheelchair/stretcher capacity need ramp or lift equipment")
        return self


class VehicleUpdate(BaseModel):
    unit_number: str | None = Field(default=None, min_length=1, max_length=32)
    plate: str | None = Field(default=None, min_length=1, max_length=16)
    plate_state: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    vehicle_type: VehicleType | None = None
    seat_capacity: int | None = Field(default=None, ge=0, le=60)
    wheelchair_capacity: int | None = Field(default=None, ge=0, le=10)
    stretcher_capacity: int | None = Field(default=None, ge=0, le=4)
    equipment: Equipment | None = None
    status: VehicleStatus | None = None
    odometer_miles: int | None = Field(default=None, ge=0)
    registration_expires_on: date | None = None
    insurance_policy_number: str | None = Field(default=None, max_length=64)
    insurance_expires_on: date | None = None
    default_driver_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _no_nulls(self) -> "VehicleUpdate":
        required = {"unit_number", "plate", "plate_state", "vehicle_type", "seat_capacity",
                    "wheelchair_capacity", "stretcher_capacity", "equipment", "status", "odometer_miles"}
        bad = sorted(f for f in self.model_fields_set & required if getattr(self, f) is None)
        if bad:
            raise ValueError(f"cannot be null: {', '.join(bad)}")
        return self


class VehicleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    unit_number: str
    vin: str
    plate: str
    plate_state: str
    make: str
    model: str
    year: int
    vehicle_type: VehicleType
    seat_capacity: int
    wheelchair_capacity: int
    stretcher_capacity: int
    equipment: Equipment
    status: VehicleStatus
    odometer_miles: int
    registration_expires_on: date | None
    insurance_policy_number: str | None
    insurance_expires_on: date | None
    default_driver_id: UUID | None
    notes: str | None
    archived_at: datetime | None
    created_at: datetime