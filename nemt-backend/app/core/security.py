import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, MultiFernet
from fastapi import HTTPException, status

from app.core.config import settings

# ---- passwords (argon2id) -------------------------------------------------
_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


# Verified against when the user doesn't exist, so response time doesn't reveal
# whether an email is registered.
DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


# ---- JWT ---------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(UTC)


def _encode(sub: UUID, typ: str, minutes: int, **extra: str) -> str:
    now = utcnow()
    payload = {
        "sub": str(sub), "typ": typ, "iss": settings.jwt_issuer, "jti": secrets.token_hex(8),
        "iat": now, "exp": now + timedelta(minutes=minutes), **extra,
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def create_access_token(user_id: UUID, company_id: UUID, role: str) -> str:
    return _encode(user_id, "access", settings.access_token_minutes, cid=str(company_id), role=role)


def create_mfa_token(user_id: UUID, company_id: UUID) -> str:
    return _encode(user_id, "mfa", settings.mfa_token_minutes, cid=str(company_id))


def decode_token(token: str, typ: str) -> dict:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
    )
    try:
        claims = jwt.decode(
            token, settings.jwt_secret.get_secret_value(), algorithms=["HS256"],
            issuer=settings.jwt_issuer, options={"require": ["exp", "iat", "sub", "typ"]},
        )
    except jwt.PyJWTError:
        raise unauthorized from None
    if claims.get("typ") != typ:
        raise unauthorized
    return claims


# ---- opaque tokens (refresh, password reset) ---------------------------------
def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Only the hash is stored: a DB leak doesn't hand out usable tokens."""
    return hashlib.sha256(raw.encode()).hexdigest()


# ---- field-level encryption (MFA secrets now; SSN/Medicaid ID later) ------------
def _fernet() -> MultiFernet:
    keys = [k.strip() for k in settings.field_encryption_keys.split(",") if k.strip()]
    return MultiFernet([Fernet(k) for k in keys])  # first key encrypts, all keys decrypt


def encrypt_field(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_field(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ---- TOTP MFA ----------------------------------------------------------------
def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)