from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKeyConstraint, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, IdMixin, TenantMixin, new_id


class RefreshToken(IdMixin, TenantMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[UUID]
    family_id: Mapped[UUID] = mapped_column(default=new_id, index=True)  # one login = one family
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                             ondelete="CASCADE", name="fk_refresh_tokens_company_user"),
    )


class PasswordResetToken(IdMixin, TenantMixin, Base):
    __tablename__ = "password_reset_tokens"

    user_id: Mapped[UUID]
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["company_id", "user_id"], ["users.company_id", "users.id"],
                             ondelete="CASCADE", name="fk_password_reset_tokens_company_user"),
    )