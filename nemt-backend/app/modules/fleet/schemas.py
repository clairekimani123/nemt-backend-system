from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.fleet.models import (
    InspectionKind, InspectionResult, MaintenanceKind,
)


# ------------------------------------------------------------------ maintenance
class MaintenanceIn(BaseModel):
    kind: MaintenanceKind
    service_date: date
    odometer_km: int | None = Field(default=None, ge=0)
    cost_cents: int | None = Field(default=None, ge=0)
    vendor: str | None = Field(default=None, max_length=200)
    invoice_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)
    next_due_date: date | None = None
    next_due_km: int | None = Field(default=None, ge=0)

class MaintenanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    vehicle_id: UUID
    kind: MaintenanceKind
    service_date: date
    odometer_km: int | None
    cost_cents: int | None
    vendor: str | None
    invoice_number: str | None
    notes: str | None
    next_due_date: date | None
    next_due_km: int | None
    created_at: datetime


# ------------------------------------------------------------------ inspections
class InspectionIn(BaseModel):
    kind: InspectionKind
    inspected_on: date
    expires_on: date
    result: InspectionResult
    inspector_name: str | None = Field(default=None, max_length=200)
    inspector_org: str | None = Field(default=None, max_length=200)
    certificate_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _expires_after_inspected(self) -> "InspectionIn":
        if self.expires_on < self.inspected_on:
            raise ValueError("expires_on must be on or after inspected_on")
        return self

class InspectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    vehicle_id: UUID
    kind: InspectionKind
    inspected_on: date
    expires_on: date
    result: InspectionResult
    inspector_name: str | None
    inspector_org: str | None
    certificate_number: str | None
    notes: str | None
    created_at: datetime


# ------------------------------------------------------------------ mileage
class MileageIn(BaseModel):
    reading_km: int = Field(ge=0)
    recorded_at: datetime | None = None
    note: str | None = Field(default=None, max_length=200)


class MileageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    vehicle_id: UUID
    reading_km: int
    recorded_at: datetime
    source: str
    note: str | None


# ------------------------------------------------------------------ dashboard
class VehicleFleetStatus(BaseModel):
    """Everything the fleet manager needs to know about one vehicle, in one object."""
    vehicle_id: UUID
    unit_number: str
    status: str                        # active | maintenance | retired
    make: str
    model: str
    year: int
    plate: str
    current_mileage_km: int | None
    mileage_updated_at: datetime | None
    last_maintenance_date: date | None
    last_maintenance_kind: MaintenanceKind | None
    next_maintenance_due_date: date | None
    next_maintenance_due_km: int | None
    days_since_last_maintenance: int | None
    last_inspection_date: date | None
    last_inspection_result: InspectionResult | None
    next_inspection_expires_on: date | None
    registration_expires_on: date | None
    insurance_expires_on: date | None
    days_until_registration_expiry: int | None
    days_until_insurance_expiry: int | None
    days_until_inspection_expiry: int | None
    alerts: list[str]                  # ['insurance_expires_in_14_days', 'maintenance_due', ...]