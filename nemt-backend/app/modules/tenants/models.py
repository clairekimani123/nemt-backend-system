from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TimestampMixin


class Company(IdMixin, TimestampMixin, Base):
    """A tenant: one NEMT transportation company on the platform."""

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_platform: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))