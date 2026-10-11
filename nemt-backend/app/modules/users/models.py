from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TenantMixin, TimestampMixin
from app.core.permissions import Role


class User(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[Role] = mapped_column(
        Enum(Role, native_enum=False, length=32, create_constraint=True, name="user_role",
             values_callable=lambda e: [m.value for m in e])
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))

    mfa_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    mfa_secret_enc: Mapped[str | None] = mapped_column(String(512))  # Fernet-encrypted
    failed_login_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # emails are unique *per company*: the same person may exist in two tenants
        Index("uq_users_company_email", "company_id", text("lower(email)"), unique=True),
        # target for composite FKs: (company_id, user_id) makes cross-tenant links impossible
        UniqueConstraint("company_id", "id", name="uq_users_company_id_id"),
    )