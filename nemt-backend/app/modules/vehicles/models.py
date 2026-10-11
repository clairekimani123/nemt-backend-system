from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import ArchiveMixin, Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class VehicleType(StrEnum):
    SEDAN = "sedan"
    MINIVAN = "minivan"
    WHEELCHAIR_VAN = "wheelchair_van"
    STRETCHER_VAN = "stretcher_van"
    BUS = "bus"


class Equipment(StrEnum):
    NONE = "none"
    RAMP = "ramp"
    LIFT = "lift"


class VehicleStatus(StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    OUT_OF_SERVICE = "out_of_service"
    RETIRED = "retired"


class Vehicle(IdMixin, TenantMixin, TimestampMixin, ArchiveMixin, Base):
    __tablename__ = "vehicles"

    unit_number: Mapped[str] = mapped_column(String(32))
    vin: Mapped[str] = mapped_column(String(17))
    plate: Mapped[str] = mapped_column(String(16))
    plate_state: Mapped[str] = mapped_column(String(2))
    make: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(50))
    year: Mapped[int] = mapped_column(SmallInteger)
    vehicle_type: Mapped[VehicleType] = mapped_column(str_enum(VehicleType, "vehicle_type"))
    # Capacity is what the route optimiser needs (Phase: routing). Seats and wheelchair
    # positions are separate resources: a van with 2 chairs may seat only 4 walkers.
    seat_capacity: Mapped[int] = mapped_column(SmallInteger)
    wheelchair_capacity: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    stretcher_capacity: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    equipment: Mapped[Equipment] = mapped_column(str_enum(Equipment, "vehicle_equipment"), default=Equipment.NONE)
    status: Mapped[VehicleStatus] = mapped_column(str_enum(VehicleStatus, "vehicle_status"), default=VehicleStatus.ACTIVE)
    odometer_miles: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    registration_expires_on: Mapped[date | None] = mapped_column(Date)
    insurance_policy_number: Mapped[str | None] = mapped_column(String(64))
    insurance_expires_on: Mapped[date | None] = mapped_column(Date)
    default_driver_id: Mapped[UUID | None]
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_vehicles_company_id_id"),
        UniqueConstraint("company_id", "unit_number", name="uq_vehicles_company_unit"),
        UniqueConstraint("company_id", "vin", name="uq_vehicles_company_vin"),
        ForeignKeyConstraint(["company_id", "default_driver_id"], ["drivers.company_id", "drivers.id"],
                             name="fk_vehicles_company_default_driver"),
        CheckConstraint("seat_capacity >= 0 AND wheelchair_capacity >= 0 AND stretcher_capacity >= 0",
                        name="capacity_non_negative"),
        # a vehicle with wheelchair/stretcher spots must declare how people get on board
        CheckConstraint("wheelchair_capacity = 0 OR equipment <> 'none'", name="wheelchair_needs_equipment"),
        Index("ix_vehicles_company_status", "company_id", "status"),
    )