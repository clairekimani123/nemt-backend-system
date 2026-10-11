from pydantic import BaseModel, Field

from app.core.types import Email, Password


class RegisterIn(BaseModel):
    email: Email
    password: Password
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)


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