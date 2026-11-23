from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import ArchiveMixin, Base, IdMixin, TenantMixin, TimestampMixin


class DutyStatus(StrEnum):
    """What the driver says about themselves. 'On a ride' is derived from their live trips."""
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"   # on duty, but on a break / can't take more work right now
    OFF_DUTY = "off_duty"


class Driver(IdMixin, TenantMixin, TimestampMixin, ArchiveMixin, Base):
    """Operational profile. Identity/login lives in `users` (role=driver); 1:1 link."""

    __tablename__ = "drivers"

    user_id: Mapped[UUID]
    employee_number: Mapped[str | None] = mapped_column(String(32))
    license_number_enc: Mapped[str | None] = mapped_column(String(512))  # encrypted
    license_last4: Mapped[str | None] = mapped_column(String(4))
    license_state: Mapped[str | None] = mapped_column(String(2))
    license_expires_on: Mapped[date | None] = mapped_column(Date)
    medical_cert_expires_on: Mapped[date | None] = mapped_column(Date)  # DOT medical card
    wheelchair_certified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    hire_date: Mapped[date | None] = mapped_column(Date)
    emergency_contact_name: Mapped[str | None] = mapped_column(String(200))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(32))
    duty_status: Mapped[DutyStatus] = mapped_column(
        Enum(DutyStatus, native_enum=False, length=16, create_constraint=True, name="driver_duty_status",
             values_callable=lambda e: [m.value for m in e]),
        default=DutyStatus.OFF_DUTY, server_default="off_duty")
    duty_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_drivers_company_id_id"),
        UniqueConstraint("company_id", "user_id", name="uq_drivers_company_user"),
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                             name="fk_drivers_company_user"),
        Index("uq_drivers_company_employee_no", "company_id", "employee_number", unique=True,
              postgresql_where=text("employee_number IS NOT NULL")),
    )