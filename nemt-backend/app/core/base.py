from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid6 import uuid7

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def new_id() -> UUID:
    """UUIDv7: time-ordered, so inserts stay index-friendly (unlike random UUIDv4)."""
    return UUID(int=uuid7().int)


class IdMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TenantMixin:
    """Every tenant-owned table gets this. RLS policies key off `company_id`."""

    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), index=True
    )


def str_enum(cls: type, name: str, length: int = 32) -> Enum:
    """Enum stored as VARCHAR + CHECK (easy to migrate, unlike native PG enums)."""
    return Enum(cls, native_enum=False, length=length, create_constraint=True, name=name,
                values_callable=lambda e: [m.value for m in e])


class ArchiveMixin:
    """Soft delete. Medical-adjacent records must be retained (HIPAA: 6 years), so the
    app never hard-deletes them; the DB role isn't even granted DELETE."""

    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))