from datetime import date
from uuid import UUID

from sqlalchemy import Boolean, Date, ForeignKeyConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import ArchiveMixin, Base, IdMixin, TenantMixin, TimestampMixin


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

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_drivers_company_id_id"),
        UniqueConstraint("company_id", "user_id", name="uq_drivers_company_user"),
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                             name="fk_drivers_company_user"),
        Index("uq_drivers_company_employee_no", "company_id", "employee_number", unique=True,
              postgresql_where=text("employee_number IS NOT NULL")),
    )