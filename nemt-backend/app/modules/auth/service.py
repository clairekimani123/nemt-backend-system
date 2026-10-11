import logging
from datetime import timedelta
from uuid import UUID

import pyotp
from fastapi import HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base import new_id
from app.core.config import settings
from app.core.db import open_tenant_session
from app.core.permissions import Role
from app.core.security import (
    DUMMY_HASH,
    create_access_token,
    decode_token,
    decrypt_field,
    encrypt_field,
    hash_password,
    hash_token,
    needs_rehash,
    new_opaque_token,
    utcnow,
    verify_password,
    verify_totp,
)
from app.modules.audit import service as audit
from app.modules.auth.models import PasswordResetToken, RefreshToken
from app.modules.auth.schemas import RegisterIn, TokenOut
from app.modules.users.models import User

log = logging.getLogger(__name__)


def _invalid() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")


def _locked() -> HTTPException:
    return HTTPException(status.HTTP_423_LOCKED, "Account temporarily locked. Try again later.")


async def get_user_by_email(db: AsyncSession, company_id: UUID, email: str) -> User | None:
    return await db.scalar(
        select(User).where(User.company_id == company_id, func.lower(User.email) == email.lower())
    )


def parse_scoped_token(raw: str) -> UUID:
    """Opaque tokens look like '<company_id>.<secret>' so they route themselves to a tenant."""
    try:
        return UUID(raw.split(".", 1)[0])
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from None


# ---- registration -----------------------------------------------------------------
async def register_passenger(
    db: AsyncSession, company_id: UUID, data: RegisterIn, request: Request
) -> User:
    if await get_user_by_email(db, company_id, data.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(
        company_id=company_id, email=data.email, password_hash=hash_password(data.password),
        full_name=data.full_name, phone=data.phone, role=Role.PASSENGER,  # role is never client-chosen
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from None
    await audit.record(db, action="auth.register", actor=user, request=request)
    await db.commit()
    return user


# ---- login ----------------------------------------------------------------------------
async def _register_failure(db: AsyncSession, user: User, request: Request, reason: str) -> None:
    user.failed_login_count += 1
    locked = user.failed_login_count >= settings.max_failed_logins
    if locked:
        user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_login_count = 0
    await audit.record(db, action="auth.login_failed", actor=user, request=request,
                       meta={"reason": reason, "locked": locked})
    await db.commit()  # persist the counter even though we are about to raise


async def authenticate(
    db: AsyncSession, company_id: UUID, email: str, password: str, request: Request
) -> User:
    user = await get_user_by_email(db, company_id, email)
    if user is None:
        verify_password(DUMMY_HASH, password)  # equalise timing
        await audit.record(db, action="auth.login_failed", company_id=company_id, request=request,
                           meta={"reason": "unknown_user"})
        await db.commit()
        raise _invalid()
    if user.locked_until and user.locked_until > utcnow():
        await audit.record(db, action="auth.login_blocked", actor=user, request=request)
        await db.commit()
        raise _locked()
    if not verify_password(user.password_hash, password):
        await _register_failure(db, user, request, "bad_password")
        raise _invalid()
    if not user.is_active:
        await audit.record(db, action="auth.login_failed", actor=user, request=request,
                           meta={"reason": "inactive"})
        await db.commit()
        raise _invalid()
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


async def issue_tokens(
    db: AsyncSession, user: User, request: Request, family_id: UUID | None = None
) -> TokenOut:
    raw = f"{user.company_id}.{new_opaque_token()}"
    db.add(
        RefreshToken(
            company_id=user.company_id, user_id=user.id, family_id=family_id or new_id(),
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    )
    return TokenOut(
        access_token=create_access_token(user.id, user.company_id, user.role.value),
        refresh_token=raw,
        expires_in=settings.access_token_minutes * 60,
    )


async def complete_login(db: AsyncSession, user: User, request: Request) -> TokenOut:
    user.failed_login_count, user.locked_until, user.last_login_at = 0, None, utcnow()
    tokens = await issue_tokens(db, user, request)
    await audit.record(db, action="auth.login", actor=user, request=request)
    await db.commit()
    return tokens


async def mfa_login(
    db: AsyncSession, company_id: UUID, mfa_token: str, code: str, request: Request
) -> TokenOut:
    claims = decode_token(mfa_token, "mfa")
    if UUID(claims["cid"]) != company_id:
        raise _invalid()
    user = await db.get(User, UUID(claims["sub"]))
    if user is None or not user.is_active or not user.mfa_enabled or not user.mfa_secret_enc:
        raise _invalid()
    if user.locked_until and user.locked_until > utcnow():
        raise _locked()
    if not verify_totp(decrypt_field(user.mfa_secret_enc), code):
        await _register_failure(db, user, request, "bad_mfa_code")
        raise _invalid()
    return await complete_login(db, user, request)


# ---- refresh-token rotation with reuse detection -------------------------------------
async def _revoke_family(db: AsyncSession, family_id: UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def rotate_refresh_token(raw: str, request: Request) -> TokenOut:
    company_id = parse_scoped_token(raw)
    async with open_tenant_session(company_id) as db:
        rt = await db.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_token(raw), RefreshToken.company_id == company_id)
            .with_for_update()
        )
        if rt is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
        if rt.revoked_at is not None:
            # An already-used token came back: assume theft, kill the whole login session.
            await _revoke_family(db, rt.family_id)
            await audit.record(db, action="auth.refresh_reuse_detected", company_id=company_id,
                               actor=None, request=request, meta={"user_id": str(rt.user_id)})
            await db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
        if rt.expires_at < utcnow():
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
        user = await db.get(User, rt.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
        rt.revoked_at = utcnow()
        tokens = await issue_tokens(db, user, request, family_id=rt.family_id)
        await db.commit()
        return tokens


async def logout(raw: str) -> None:
    company_id = parse_scoped_token(raw)
    async with open_tenant_session(company_id) as db:
        rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
        if rt is not None:
            await _revoke_family(db, rt.family_id)
            await db.commit()


async def revoke_user_sessions(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


# ---- password reset ------------------------------------------------------------------
async def forgot_password(db: AsyncSession, company_id: UUID, email: str, request: Request) -> None:
    from app.workers.tasks import send_email  # local import: keeps API import light

    user = await get_user_by_email(db, company_id, email)
    if user is None or not user.is_active:
        return  # same response either way: no account enumeration
    raw = f"{company_id}.{new_opaque_token()}"
    db.add(PasswordResetToken(
        company_id=company_id, user_id=user.id, token_hash=hash_token(raw),
        expires_at=utcnow() + timedelta(minutes=settings.password_reset_minutes),
    ))
    await audit.record(db, action="auth.password_reset_requested", actor=user, request=request)
    await db.commit()
    link = f"{settings.frontend_url}/reset-password?token={raw}"
    try:
        send_email.delay(
            user.email, f"{settings.app_name}: reset your password",
            f"Use this link to reset your password (valid {settings.password_reset_minutes} min):\n{link}",
        )
    except Exception:  # broker down must not change the response
        log.exception("could not enqueue password reset email")


async def reset_password(raw: str, new_password: str, request: Request) -> None:
    company_id = parse_scoped_token(raw)
    async with open_tenant_session(company_id) as db:
        prt = await db.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == hash_token(raw),
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > utcnow(),
            )
        )
        user = await db.get(User, prt.user_id) if prt else None
        if prt is None or user is None or not user.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
        user.password_hash = hash_password(new_password)
        user.failed_login_count, user.locked_until = 0, None
        prt.used_at = utcnow()
        await db.execute(  # sign the user out everywhere
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        await audit.record(db, action="auth.password_reset", actor=user, request=request)
        await db.commit()


# ---- MFA enrolment ---------------------------------------------------------------------
async def mfa_setup(db: AsyncSession, user: User, request: Request) -> tuple[str, str]:
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA already enabled")
    secret = pyotp.random_base32()
    user.mfa_secret_enc = encrypt_field(secret)
    await audit.record(db, action="auth.mfa_setup_started", actor=user, request=request)
    await db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=settings.app_name)
    return secret, uri


async def mfa_enable(db: AsyncSession, user: User, code: str, request: Request) -> None:
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA already enabled")
    if not user.mfa_secret_enc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Run MFA setup first")
    if not verify_totp(decrypt_field(user.mfa_secret_enc), code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid code")
    user.mfa_enabled = True
    await audit.record(db, action="auth.mfa_enabled", actor=user, request=request)
    await db.commit()