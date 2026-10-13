"""Route optimisation tables.

A `RoutePlan` is a proposal: what the solver would do if you let it, without touching
anything. `Route`s are the vehicles it wants to use that day, and `RouteStop`s are the
ordered stops on each. Applying a plan is what actually assigns trips; discarding one
leaves only an audit trail.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class PlanStatus(StrEnum):
    PROPOSED = "proposed"       # freshly generated, not yet applied
    APPLIED = "applied"         # trips have been assigned
    DISCARDED = "discarded"     # the dispatcher rejected it


class RouteStatus(StrEnum):
    PROPOSED = "proposed"       # still part of a proposed plan
    PUBLISHED = "published"     # applied: trips on this route are assigned
    SUPERSEDED = "superseded"   # a later plan replaced this route for the same day
    DISCARDED = "discarded"     # plan was discarded before applying


class RoutePlan(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "route_plans"

    route_date: Mapped[date] = mapped_column(Date)
    status: Mapped[PlanStatus] = mapped_column(
        str_enum(PlanStatus, "plan_status"), default=PlanStatus.PROPOSED
    )

    # solver inputs / outputs, stored so a proposal can be reproduced and audited
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    unassigned: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    skipped_resources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)

    created_by: Mapped[UUID | None]
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[UUID | None]

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_route_plans_company_id_id"),
        Index("ix_route_plans_company_date", "company_id", "route_date"),
        Index("ix_route_plans_company_status", "company_id", "status"),
    )


class Route(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "routes"

    plan_id: Mapped[UUID]
    route_date: Mapped[date] = mapped_column(Date)
    driver_id: Mapped[UUID]
    vehicle_id: Mapped[UUID]
    status: Mapped[RouteStatus] = mapped_column(
        str_enum(RouteStatus, "route_status"), default=RouteStatus.PROPOSED
    )
    planned_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    drive_minutes: Mapped[int] = mapped_column(Integer, default=0)
    distance_m: Mapped[int] = mapped_column(BigInteger, default=0)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_routes_company_id_id"),
        ForeignKeyConstraint(["company_id", "plan_id"], ["route_plans.company_id", "route_plans.id"],
                             name="fk_routes_company_plan", ondelete="CASCADE"),
        ForeignKeyConstraint(["company_id", "driver_id"], ["drivers.company_id", "drivers.id"],
                             name="fk_routes_company_driver"),
        ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"],
                             name="fk_routes_company_vehicle"),
        CheckConstraint("planned_end_at > planned_start_at", name="ck_routes_end_after_start"),
        Index("ix_routes_company_date", "company_id", "route_date"),
        Index("ix_routes_company_status", "company_id", "status"),
        Index("ix_routes_company_driver_date", "company_id", "driver_id", "route_date"),
    )


class RouteStop(IdMixin, TenantMixin, Base):
    """Append-only: a stop is a fact, once written it never changes."""

    __tablename__ = "route_stops"

    route_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(SmallInteger)
    trip_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(8))       # "pickup" | "dropoff"

    planned_arrival_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    wait_minutes: Mapped[int] = mapped_column(SmallInteger, default=0)

    # vehicle load AFTER this stop
    load_seats: Mapped[int] = mapped_column(SmallInteger, default=0)
    load_wheelchair: Mapped[int] = mapped_column(SmallInteger, default=0)
    load_stretcher: Mapped[int] = mapped_column(SmallInteger, default=0)

    # the point this stop happens at (denormalized from the trip for map rendering)
    latitude: Mapped[float]
    longitude: Mapped[float]

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "route_id"], ["routes.company_id", "routes.id"],
                             name="fk_route_stops_company_route", ondelete="CASCADE"),
        ForeignKeyConstraint(["company_id", "trip_id"], ["trips.company_id", "trips.id"],
                             name="fk_route_stops_company_trip"),
        CheckConstraint("kind IN ('pickup', 'dropoff')", name="ck_route_stops_kind"),
        CheckConstraint("sequence > 0", name="ck_route_stops_sequence_positive"),
        CheckConstraint("planned_departure_at >= planned_arrival_at", name="ck_route_stops_departure_after_arrival"),
        Index("ix_route_stops_route_seq", "route_id", "sequence"),
        Index("ix_route_stops_company_trip", "company_id", "trip_id"),
    )