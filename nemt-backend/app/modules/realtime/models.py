"""Driver position pings: append-only.

Every location a driver's phone reports while a trip is active is written here. It's the
dispatcher's live map, the billing audit's proof of service, and a record that survives
a driver losing their phone. Never updated, never deleted.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import BigInteger, Computed, DateTime, Float, ForeignKeyConstraint, Identity, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class DriverPosition(TenantMixin, Base):
    __tablename__ = "driver_positions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    driver_id: Mapped[UUID]
    trip_id: Mapped[UUID | None]
    location: Mapped[Any] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    latitude: Mapped[float] = mapped_column(Float, Computed("ST_Y(location::geometry)", persisted=True))
    longitude: Mapped[float] = mapped_column(Float, Computed("ST_X(location::geometry)", persisted=True))
    speed_mps: Mapped[float | None]
    heading_deg: Mapped[float | None]
    accuracy_m: Mapped[float | None]
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

   
    __table_args__ = (
        ForeignKeyConstraint(["company_id", "driver_id"], ["drivers.company_id", "drivers.id"],
                             name="fk_driver_positions_company_driver", ondelete="CASCADE"),
        ForeignKeyConstraint(["company_id", "trip_id"], ["trips.company_id", "trips.id"],
                             name="fk_driver_positions_company_trip"),
        Index("ix_driver_positions_driver_time", "driver_id", "recorded_at"),
        Index("ix_driver_positions_trip_time", "trip_id", "recorded_at",
              postgresql_where=text("trip_id IS NOT NULL")),
    )