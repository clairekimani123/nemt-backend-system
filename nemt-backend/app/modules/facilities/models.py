from enum import StrEnum

from sqlalchemy import Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.address import AddressMixin
from app.core.base import ArchiveMixin, Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class FacilityKind(StrEnum):
    HOSPITAL = "hospital"
    CLINIC = "clinic"
    DIALYSIS_CENTER = "dialysis_center"
    NURSING_HOME = "nursing_home"
    ASSISTED_LIVING = "assisted_living"
    REHAB = "rehab"
    DAY_PROGRAM = "day_program"
    OTHER = "other"


class Facility(IdMixin, TenantMixin, TimestampMixin, ArchiveMixin, AddressMixin, Base):
    """Places patients travel to/from, and clients who book rides for them."""

    __tablename__ = "facilities"

    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[FacilityKind] = mapped_column(str_enum(FacilityKind, "facility_kind"))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    transportation_requirements: Mapped[str | None] = mapped_column(Text)
    billing_contact_email: Mapped[str | None] = mapped_column(String(320))
    billing_notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_facilities_company_id_id"),
        Index("ix_facilities_company_name", "company_id", "name"),
        Index("ix_facilities_location", "location", postgresql_using="gist"),
        Index("ix_facilities_name_trgm", text("(lower(name)) gin_trgm_ops"), postgresql_using="gin"),
    )


class Broker(IdMixin, TenantMixin, TimestampMixin, ArchiveMixin, Base):
    """NEMT brokers / managed-care payers that send us trips (ModivCare, MTM, ...).
    Billing rules and import formats attach here in the Billing/Integrations phases."""

    __tablename__ = "brokers"

    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(32))  # short stable id, e.g. MODIVCARE
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_brokers_company_id_id"),
        UniqueConstraint("company_id", "code", name="uq_brokers_company_code"),
    )