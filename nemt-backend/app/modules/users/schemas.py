from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.permissions import Role
from app.core.types import Email, Password


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    company_id: UUID
    email: str
    full_name: str
    phone: str | None
    role: Role
    is_active: bool
    mfa_enabled: bool
    last_login_at: datetime | None
    must_change_password: bool = False
    has_avatar: bool = False
    avatar_updated_at: datetime | None = None  # changes on every new photo: use it to bust caches
    created_at: datetime


class UserCreate(BaseModel):
    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    role: Role


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    role: Role | None = None
    is_active: bool | None = None

class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: Password


class AdminPasswordResetIn(BaseModel):
    """`admin_password` is the ADMINISTRATOR's own password (step-up check), never the user's."""
    mode: Literal["email_link", "temporary_password"]
    temporary_password: Password | None = None
    admin_password: str = Field(max_length=128)

    @model_validator(mode="after")
    def _temporary_needs_password(self) -> "AdminPasswordResetIn":
        if (self.mode == "temporary_password") != (self.temporary_password is not None):
            raise ValueError("temporary_password is required for mode 'temporary_password' and only then")
        return self


class AdminPasswordResetOut(BaseModel):
    mode: str
    sessions_revoked: bool = True
    email_queued: bool | None = None  # queued for the mail worker; delivery is not confirmed here
    must_change_password: bool


class StepUpIn(BaseModel):
    admin_password: str = Field(max_length=128)
