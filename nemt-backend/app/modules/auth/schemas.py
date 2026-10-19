from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.core.address import AddressIn
from app.core.types import Email, Password
from app.modules.patients.models import MobilityType


class RegisterIn(BaseModel):
    """Passenger self-registration. Creates the login AND the passenger (patient) profile that
    dispatch works with, so a new passenger is immediately visible to the company."""

    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    date_of_birth: date  # needed by dispatch (minors must travel with an escort)
    mobility: MobilityType = MobilityType.AMBULATORY
    home_address: AddressIn | None = None

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("full_name cannot be blank")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def _dob(cls, v: date) -> date:
        if v > date.today() or v.year < 1900:
            raise ValueError("date_of_birth out of range")
        return v


class LoginIn(BaseModel):
    email: Email
    password: str = Field(max_length=128)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class LoginOut(BaseModel):
    """Either tokens, or `mfa_required` + a short-lived `mfa_token` for step two."""

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    mfa_required: bool = False
    mfa_token: str | None = None


class MfaLoginIn(BaseModel):
    mfa_token: str
    code: str = Field(min_length=6, max_length=6)


class RefreshIn(BaseModel):
    refresh_token: str


class ForgotIn(BaseModel):
    email: Email


class ResetIn(BaseModel):
    token: str
    new_password: Password


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str  # render as a QR code in the frontend


class MfaEnableIn(BaseModel):
    code: str = Field(min_length=6, max_length=6)