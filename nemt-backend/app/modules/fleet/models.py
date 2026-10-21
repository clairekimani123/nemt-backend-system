"""Fleet records: maintenance, inspections, mileage.

Three append-only tables that track the life of each vehicle. Together with
Vehicle.registration_expires_on and Vehicle.insurance_expires_on, this gives the
fleet manager a complete compliance picture:

  - When was the last oil change? At what mileage?
  - When is the next state inspection due?
  - How many miles has this van driven this month?
  - Is the insurance still valid?

Maintenance and inspections are historical facts: never updated, only appended to
(and corrected by a follow-up record if a mistake was made). Mileage is the same.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class MaintenanceKind(StrEnum):
    OIL_CHANGE = "oil_change"
    TIRE_ROTATION = "tire_rotation"
    TIRE_REPLACEMENT = "tire_replacement"
    BRAKE_SERVICE = "brake_service"
    TRANSMISSION = "transmission"
    BATTERY = "battery"
    AC_SERVICE = "ac_service"
    LIFT_INSPECTION = "lift_inspection"       # wheelchair lift service
    RAMP_INSPECTION = "ramp_inspection"       # wheelchair ramp service
    GENERAL_REPAIR = "general_repair"
    OTHER = "other"


class InspectionKind(StrEnum):
    STATE_ANNUAL = "state_annual"             # state safety inspection
    INSURANCE_ANNUAL = "insurance_annual"
    INTERNAL_MONTHLY = "internal_monthly"
    ACCESSIBILITY = "accessibility"           # ADA-related
    OTHER = "other"


class InspectionResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"                             # vehicle is out of service until a follow-up pass


class VehicleMaintenance(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "vehicle_maintenance"

    vehicle_id: Mapped[UUID]
    kind: Mapped[MaintenanceKind] = mapped_column(str_enum(MaintenanceKind, "maintenance_kind"))
    service_date: Mapped[date] = mapped_column(Date)
    odometer_km: Mapped[int | None] = mapped_column(Integer)
    cost_cents: Mapped[int | None] = mapped_column(Integer)
    vendor: Mapped[str | None] = mapped_column(String(200))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    next_due_date: Mapped[date | None] = mapped_column(Date)
    next_due_km: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[UUID | None]

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_vehicle_maintenance_company_id_id"),
        ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                             name="fk_vehicle_maintenance_company_vehicle", ondelete="CASCADE"),
        CheckConstraint("cost_cents IS NULL OR cost_cents >= 0", name="ck_maint_cost_positive"),
        CheckConstraint("odometer_km IS NULL OR odometer_km >= 0", name="ck_maint_odo_positive"),
        Index("ix_maint_company_vehicle_date", "company_id", "vehicle_id", "service_date"),
        Index("ix_maint_company_next_due", "company_id", "next_due_date"),
    )


class VehicleInspection(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "vehicle_inspections"

    vehicle_id: Mapped[UUID]
    kind: Mapped[InspectionKind] = mapped_column(str_enum(InspectionKind, "inspection_kind"))
    inspected_on: Mapped[date] = mapped_column(Date)
    expires_on: Mapped[date] = mapped_column(Date)
    result: Mapped[InspectionResult] = mapped_column(str_enum(InspectionResult, "inspection_result"))
    inspector_name: Mapped[str | None] = mapped_column(String(200))
    inspector_org: Mapped[str | None] = mapped_column(String(200))
    certificate_number: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None]

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_vehicle_inspections_company_id_id"),
        ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                             name="fk_vehicle_inspections_company_vehicle", ondelete="CASCADE"),
        CheckConstraint("expires_on >= inspected_on", name="ck_insp_expires_after"),
        Index("ix_insp_company_vehicle_date", "company_id", "vehicle_id", "inspected_on"),
        Index("ix_insp_company_expires", "company_id", "expires_on"),
    )


class VehicleMileage(TenantMixin, Base):
    """A single odometer reading. Append-only.

    Sources:
      - 'manual'    — the driver or fleet manager typed the reading
      - 'trip_derived' — computed from a completed trip's distance_km
      - 'maintenance' — recorded as part of a maintenance job

    Multiple sources can produce readings on the same day; that's fine. The most
    recent reading (highest recorded_at) is the "current mileage."
    """

    __tablename__ = "vehicle_mileage"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vehicle_id: Mapped[UUID]
    reading_km: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[UUID | None]

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                             name="fk_vehicle_mileage_company_vehicle", ondelete="CASCADE"),
        CheckConstraint("reading_km >= 0", name="ck_mileage_reading_positive"),
        Index("ix_mileage_company_vehicle_time", "company_id", "vehicle_id", "recorded_at"),
    )