from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.address import AddressMixin
from app.core.base import ArchiveMixin, Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class MobilityType(StrEnum):
    AMBULATORY = "ambulatory"
    WALKER = "walker"
    WHEELCHAIR_MANUAL = "wheelchair_manual"
    WHEELCHAIR_POWER = "wheelchair_power"
    STRETCHER = "stretcher"


class AssistanceLevel(StrEnum):
    CURB_TO_CURB = "curb_to_curb"
    DOOR_TO_DOOR = "door_to_door"
    DOOR_THROUGH_DOOR = "door_through_door"


class Patient(IdMixin, TenantMixin, TimestampMixin, ArchiveMixin, Base):
    __tablename__ = "patients"

    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(320))
    preferred_language: Mapped[str] = mapped_column(String(8), default="en", server_default="en")

    # --- encrypted at the field level (Fernet) ---
    date_of_birth_enc: Mapped[str] = mapped_column(String(512))
    medicaid_id_enc: Mapped[str | None] = mapped_column(String(512))
    notes_enc: Mapped[str | None] = mapped_column(Text)
    # --- derived, so lists/search never need to decrypt ---
    medicaid_id_hash: Mapped[str | None] = mapped_column(String(64))  # HMAC blind index
    medicaid_last4: Mapped[str | None] = mapped_column(String(4))

    mobility: Mapped[MobilityType] = mapped_column(str_enum(MobilityType, "patient_mobility"))
    assistance_level: Mapped[AssistanceLevel] = mapped_column(str_enum(AssistanceLevel, "patient_assistance"))
    requires_escort: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    uses_oxygen: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    has_service_animal: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    weight_lbs: Mapped[int | None] = mapped_column(SmallInteger)

    companion_name: Mapped[str | None] = mapped_column(String(200))
    companion_phone: Mapped[str | None] = mapped_column(String(32))
    companion_relationship: Mapped[str | None] = mapped_column(String(50))
    emergency_contact_name: Mapped[str | None] = mapped_column(String(200))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(32))

    user_id: Mapped[UUID | None]  # optional passenger-portal login

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_patients_company_id_id"),
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                             name="fk_patients_company_user"),
        # same Medicaid ID twice in one company = duplicate patient
        Index("uq_patients_company_medicaid", "company_id", "medicaid_id_hash", unique=True,
              postgresql_where=text("medicaid_id_hash IS NOT NULL")),
        Index("uq_patients_company_user", "company_id", "user_id", unique=True,
              postgresql_where=text("user_id IS NOT NULL")),
        Index("ix_patients_company_last_name", "company_id", "last_name"),
        # fast "%smi%" name search (pg_trgm)
        Index("ix_patients_name_trgm", text("(lower(first_name || ' ' || last_name))"),
      postgresql_using="gin",
      postgresql_ops={"lower(first_name || ' ' || last_name)": "gin_trgm_ops"})
    )


class PatientAddress(IdMixin, TenantMixin, TimestampMixin, AddressMixin, Base):
    """Home, dialysis centre, day programme... a patient has several pickup places."""

    __tablename__ = "patient_addresses"

    patient_id: Mapped[UUID]
    label: Mapped[str] = mapped_column(String(50))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    access_notes: Mapped[str | None] = mapped_column(String(500))  # gate code, buzzer, etc.
    geocoded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "patient_id"], ["patients.company_id", "patients.id"],
                             ondelete="CASCADE", name="fk_patient_addresses_company_patient"),
        Index("uq_patient_addresses_one_primary", "patient_id", unique=True,
              postgresql_where=text("is_primary")),
        Index("ix_patient_addresses_patient", "patient_id"),
        Index("ix_patient_addresses_location", "location", postgresql_using="gist"),
    )