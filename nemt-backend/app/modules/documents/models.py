from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import ArchiveMixin, Base, IdMixin, TenantMixin, TimestampMixin, str_enum


class OwnerType(StrEnum):
    PATIENT = "patient"
    DRIVER = "driver"
    VEHICLE = "vehicle"
    FACILITY = "facility"


class DocType(StrEnum):
    LICENSE = "license"
    MEDICAL_CERT = "medical_cert"
    CERTIFICATION = "certification"
    INSURANCE = "insurance"
    REGISTRATION = "registration"
    INSPECTION = "inspection"
    CONSENT = "consent"
    ID_CARD = "id_card"
    CONTRACT = "contract"
    OTHER = "other"


class Document(IdMixin, TenantMixin, TimestampMixin, ArchiveMixin, Base):
    """File metadata. Bytes live in the Storage port (encrypted); the DB never holds them.

    'Exclusive arc': one nullable FK per possible owner plus a CHECK that exactly one is
    set. Unlike a generic (owner_type, owner_id) pair, the database enforces real foreign
    keys, including the company_id match.
    """

    __tablename__ = "documents"

    patient_id: Mapped[UUID | None]
    driver_id: Mapped[UUID | None]
    vehicle_id: Mapped[UUID | None]
    facility_id: Mapped[UUID | None]

    doc_type: Mapped[DocType] = mapped_column(str_enum(DocType, "document_type"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(100), unique=True)
    expires_on: Mapped[date | None] = mapped_column(Date)
    uploaded_by: Mapped[UUID | None]

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "patient_id"], ["patients.company_id", "patients.id"], name="fk_documents_company_patient"),
        ForeignKeyConstraint(["company_id", "driver_id"], ["drivers.company_id", "drivers.id"], name="fk_documents_company_driver"),
        ForeignKeyConstraint(["company_id", "vehicle_id"], ["vehicles.company_id", "vehicles.id"], name="fk_documents_company_vehicle"),
        ForeignKeyConstraint(["company_id", "facility_id"], ["facilities.company_id", "facilities.id"], name="fk_documents_company_facility"),
        CheckConstraint("num_nonnulls(patient_id, driver_id, vehicle_id, facility_id) = 1", name="exactly_one_owner"),
        Index("ix_documents_patient", "patient_id"),
        Index("ix_documents_driver", "driver_id"),
        Index("ix_documents_vehicle", "vehicle_id"),
        Index("ix_documents_facility", "facility_id"),
        Index("ix_documents_expires", "company_id", "expires_on"),
    )

    @property
    def owner_type(self) -> OwnerType:
        for t in OwnerType:
            if getattr(self, f"{t.value}_id") is not None:
                return t
        raise ValueError("document without owner")

    @property
    def owner_id(self) -> UUID:
        return getattr(self, f"{self.owner_type.value}_id")