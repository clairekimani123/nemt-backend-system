from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TenantMixin, TimestampMixin, str_enum
from app.modules.patients.models import MobilityType
from app.modules.trips.state import CancelReason, Leg, TripSource, TripStatus


def _place(prefix: str) -> type:
    """Mixin giving a table a full address + map point under a prefix ('pickup_line1', ...).

    A trip stores a SNAPSHOT of its pickup and drop-off addresses (not a link to the patient's
    address row): if the patient moves next month, last month's trip and its bill must still
    show where the ride really went. The point is NOT NULL: a trip we can't place on a map
    can't be routed, so we refuse to create it.
    """
    cols: dict[str, Any] = {
        f"{prefix}_line1": mapped_column(String(200), nullable=False),
        f"{prefix}_line2": mapped_column(String(200), nullable=True),
        f"{prefix}_city": mapped_column(String(100), nullable=False),
        f"{prefix}_state": mapped_column(String(2), nullable=False),
        f"{prefix}_postal_code": mapped_column(String(10), nullable=False),
        f"{prefix}_location": mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=False),
        f"{prefix}_latitude": mapped_column(Float, Computed(f"ST_Y({prefix}_location::geometry)", persisted=True)),
        f"{prefix}_longitude": mapped_column(Float, Computed(f"ST_X({prefix}_location::geometry)", persisted=True)),
    }
    return type(f"{prefix.title()}Place", (), cols)


PickupPlace = _place("pickup")
DropoffPlace = _place("dropoff")


class Counter(Base):
    """Per-company running numbers (trip 1, 2, 3...). One row per (company, name)."""

    __tablename__ = "counters"

    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class RecurringTrip(IdMixin, TenantMixin, TimestampMixin, PickupPlace, DropoffPlace, Base):
    """A standing order, e.g. 'dialysis Mon/Wed/Fri 07:30'. It is a RECIPE: real trips are
    cooked from it ahead of time (see service.materialize) and are then ordinary trips."""

    __tablename__ = "recurring_trips"

    patient_id: Mapped[UUID]
    broker_id: Mapped[UUID | None]
    authorization_number: Mapped[str | None] = mapped_column(String(64))
    rrule: Mapped[str] = mapped_column(String(200))          # e.g. FREQ=WEEKLY;BYDAY=MO,WE,FR
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))        # snapshot of the company's zone
    pickup_time: Mapped[time] = mapped_column(Time)          # LOCAL wall-clock time
    appointment_time: Mapped[time | None] = mapped_column(Time)
    return_pickup_time: Mapped[time | None] = mapped_column(Time)
    estimated_duration_min: Mapped[int] = mapped_column(SmallInteger)
    mobility: Mapped[MobilityType] = mapped_column(str_enum(MobilityType, "series_mobility"))
    escort_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    uses_oxygen: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    has_service_animal: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    special_instructions_enc: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    materialized_through: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[UUID | None]

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_recurring_trips_company_id_id"),
        ForeignKeyConstraint(["company_id", "patient_id"], ["patients.company_id", "patients.id"],
                             name="fk_recurring_trips_company_patient"),
        ForeignKeyConstraint(["company_id", "broker_id"], ["brokers.company_id", "brokers.id"],
                             name="fk_recurring_trips_company_broker"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="end_after_start"),
        CheckConstraint("escort_count BETWEEN 0 AND 3", name="escorts_range"),
        Index("ix_recurring_trips_company_active", "company_id", "is_active"),
    )


class Trip(IdMixin, TenantMixin, TimestampMixin, PickupPlace, DropoffPlace, Base):
    __tablename__ = "trips"

    trip_number: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[TripStatus] = mapped_column(str_enum(TripStatus, "trip_status"), default=TripStatus.SCHEDULED)
    leg: Mapped[Leg] = mapped_column(str_enum(Leg, "trip_leg"), default=Leg.OUTBOUND)
    source: Mapped[TripSource] = mapped_column(str_enum(TripSource, "trip_source"), default=TripSource.DISPATCHER)

    patient_id: Mapped[UUID]
    broker_id: Mapped[UUID | None]
    authorization_number: Mapped[str | None] = mapped_column(String(64))
    driver_id: Mapped[UUID | None]
    vehicle_id: Mapped[UUID | None]
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_by: Mapped[UUID | None]

    # When. All instants are UTC; `ends_at` = pickup + estimated duration (kept in step by the service).
    scheduled_pickup_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    appointment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_duration_min: Mapped[int] = mapped_column(SmallInteger)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # What the ride needs (copied from the patient at booking time, then adjustable per trip).
    mobility: Mapped[MobilityType] = mapped_column(str_enum(MobilityType, "trip_mobility"))
    escort_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    uses_oxygen: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    has_service_animal: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    special_instructions_enc: Mapped[str | None] = mapped_column(Text)  # free text may hold PHI -> encrypted

    # Links
    return_of_trip_id: Mapped[UUID | None]
    series_id: Mapped[UUID | None]
    occurrence_date: Mapped[date | None] = mapped_column(Date)  # which day of the series this trip is
    created_by: Mapped[UUID | None]

    # Milestones (the full detail lives in trip_events; these are the ones billing/dispatch query)
    en_route_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrived_pickup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    picked_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrived_dropoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    no_show_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason_code: Mapped[CancelReason | None] = mapped_column(str_enum(CancelReason, "trip_cancel_reason"))
    late_cancel: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_trips_company_id_id"),
        UniqueConstraint("company_id", "trip_number", name="uq_trips_company_number"),
        ForeignKeyConstraint(["company_id", "patient_id"], ["patients.company_id", "patients.id"], name="fk_trips_company_patient"),
        ForeignKeyConstraint(["company_id", "broker_id"], ["brokers.company_id", "brokers.id"], name="fk_trips_company_broker"),
        ForeignKeyConstraint(["company_id", "driver_id"], ["drivers.company_id", "drivers.id"], name="fk_trips_company_driver"),
        ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"], name="fk_trips_company_vehicle"),
        ForeignKeyConstraint(["company_id", "return_of_trip_id"], ["trips.company_id", "trips.id"], name="fk_trips_company_return_of"),
        ForeignKeyConstraint(["company_id", "series_id"], ["recurring_trips.company_id", "recurring_trips.id"], name="fk_trips_company_series"),
        # ---- rules the DATABASE enforces, even if application code has a bug ----
        CheckConstraint("ends_at > scheduled_pickup_at", name="ends_after_pickup"),
        CheckConstraint("appointment_at IS NULL OR appointment_at > scheduled_pickup_at", name="appointment_after_pickup"),
        CheckConstraint("escort_count BETWEEN 0 AND 3", name="escorts_range"),
        CheckConstraint("(driver_id IS NULL) = (vehicle_id IS NULL)", name="driver_and_vehicle_together"),
        CheckConstraint("status <> 'scheduled' OR driver_id IS NULL", name="scheduled_has_no_assignment"),
        CheckConstraint("status IN ('scheduled', 'cancelled') OR driver_id IS NOT NULL", name="active_trip_needs_driver"),
        CheckConstraint("status <> 'cancelled' OR cancel_reason_code IS NOT NULL", name="cancel_needs_reason"),
        Index("ix_trips_company_pickup", "company_id", "scheduled_pickup_at"),
        Index("ix_trips_company_status", "company_id", "status"),
        Index("ix_trips_driver_time", "driver_id", "scheduled_pickup_at", postgresql_where=text("driver_id IS NOT NULL")),
        Index("ix_trips_vehicle_time", "vehicle_id", "scheduled_pickup_at", postgresql_where=text("vehicle_id IS NOT NULL")),
        Index("ix_trips_patient_time", "patient_id", "scheduled_pickup_at"),
        # a recurring series can never create the same day+leg twice (even if the job runs twice)
        Index("uq_trips_series_occurrence", "series_id", "occurrence_date", "leg", unique=True,
              postgresql_where=text("series_id IS NOT NULL")),
    )


class TripEvent(TenantMixin, Base):
    """The trip's history book. Append-only (the app role has INSERT + SELECT, nothing else)."""

    __tablename__ = "trip_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trip_id: Mapped[UUID]
    event_type: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor_user_id: Mapped[UUID | None]
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # where the driver's phone was when they tapped the button: proof of service for billing audits
    location: Mapped[Any | None] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    latitude: Mapped[float | None] = mapped_column(Float, Computed("ST_Y(location::geometry)", persisted=True))
    longitude: Mapped[float | None] = mapped_column(Float, Computed("ST_X(location::geometry)", persisted=True))
    reason_code: Mapped[str | None] = mapped_column(String(40))
    note_enc: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "trip_id"], ["trips.company_id", "trips.id"], name="fk_trip_events_company_trip"),
        Index("ix_trip_events_trip", "trip_id", "id"),
    )