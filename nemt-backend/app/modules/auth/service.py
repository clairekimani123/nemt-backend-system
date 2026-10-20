import logging
from datetime import timedelta
from uuid import UUID

import pyotp
from fastapi import HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.address import apply_address
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
from app.modules.patients import service as patient_service
from app.modules.patients.models import AssistanceLevel, Patient, PatientAddress
from app.modules.realtime.manager import manager
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
def split_name(full_name: str) -> tuple[str, str]:
    first, _, last = full_name.strip().rpartition(" ")
    return (first, last) if first else (last, "")


async def register_passenger(
    db: AsyncSession, company_id: UUID, data: RegisterIn, request: Request
) -> tuple[User, Patient]:
    """Login + passenger profile in ONE transaction: either both exist or neither does."""
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
    first, last = split_name(data.full_name)
    patient = Patient(company_id=company_id, user_id=user.id)
    patient_service.apply_fields(patient, {
        "first_name": first[:100], "last_name": last[:100], "date_of_birth": data.date_of_birth,
        "phone": data.phone, "email": data.email, "mobility": data.mobility,
        "assistance_level": AssistanceLevel.DOOR_TO_DOOR,
    })
    db.add(patient)
    await db.flush()
    if data.home_address:
        addr = PatientAddress(company_id=company_id, patient_id=patient.id, label="home", is_primary=True)
        await apply_address(addr, data.home_address)
        db.add(addr)
    await audit.record(db, action="auth.register", actor=user, request=request,
                       resource_type="patient", resource_id=patient.id)
    await db.commit()
    return user, patient


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
        # Only reached with the CORRECT password, so this reveals nothing to someone guessing.
        await audit.record(db, action="auth.login_failed", actor=user, request=request,
                           meta={"reason": "inactive"})
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "This account has been deactivated. Please contact your company administrator.")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


async def issue_tokens(
    db: AsyncSession, user: User, request: Request, family_id: UUID | None = None
) -> TokenOut:
    raw = f"{user.company_id}.{new_opaque_token()}"
    family_id = family_id or new_id()
    db.add(
        RefreshToken(
            company_id=user.company_id, user_id=user.id, family_id=family_id,
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "")[:300],
        )
    )
    return TokenOut(
        access_token=create_access_token(user.id, user.company_id, user.role.value, family_id),
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
    """Ends ONE login session (this tab/device): its refresh tokens, its access tokens (they
    carry the session id) and its live sockets. The user's other sessions are untouched."""
    company_id = parse_scoped_token(raw)
    async with open_tenant_session(company_id) as db:
        rt = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
        if rt is not None:
            await _revoke_family(db, rt.family_id)
            await db.commit()
            await manager.close_session(company_id, rt.family_id)


async def session_active(db: AsyncSession, user_id: UUID, session_id: UUID) -> bool:
    """A login session is live while its family has an unrevoked, unexpired refresh token."""
    return await db.scalar(select(
        select(RefreshToken.id).where(
            RefreshToken.family_id == session_id, RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None), RefreshToken.expires_at > utcnow(),
        ).exists()
    )) or False


async def revoke_user_sessions(db: AsyncSession, user: User, *, keep_session: UUID | None = None) -> None:
    """Sign the user out everywhere (optionally except the session making the request). Access
    tokens die with their session; open sockets are closed now."""
    q = update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
    if keep_session is not None:
        q = q.where(RefreshToken.family_id != keep_session)
    await db.execute(q.values(revoked_at=utcnow()))
    if keep_session is None:
        await manager.close_user(user.company_id, user.id)


# ---- password reset ------------------------------------------------------------------
async def forgot_password(db: AsyncSession, company_id: UUID, email: str, request: Request) -> None:
    user = await get_user_by_email(db, company_id, email)
    if user is None or not user.is_active:
        return  # same response either way: no account enumeration
    raw = await issue_reset_token(db, user)
    await audit.record(db, action="auth.password_reset_requested", actor=user, request=request)
    await db.commit()
    queue_reset_email(user, raw)


async def _void_reset_tokens(db: AsyncSession, user_id: UUID) -> None:
    await db.execute(update(PasswordResetToken)
                     .where(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
                     .values(used_at=utcnow()))


async def issue_reset_token(db: AsyncSession, user: User) -> str:
    """A new single-use reset link token; any older unused link for this user stops working."""
    await _void_reset_tokens(db, user.id)
    raw = f"{user.company_id}.{new_opaque_token()}"
    db.add(PasswordResetToken(
        company_id=user.company_id, user_id=user.id, token_hash=hash_token(raw),
        expires_at=utcnow() + timedelta(minutes=settings.password_reset_minutes),
    ))
    return raw


def queue_reset_email(user: User, raw: str) -> bool:
    """Hand the link to the mail worker. True = queued (NOT the same as delivered)."""
    from app.workers.tasks import send_email  # local import: keeps API import light

    link = f"{settings.frontend_url}/reset-password?token={raw}"
    try:
        send_email.delay(
            user.email, f"{settings.app_name}: reset your password",
            f"Use this link to reset your password (valid {settings.password_reset_minutes} min):\n{link}",
        )
        return True
    except Exception:  # broker down must not change the response
        log.exception("could not enqueue password reset email")
        return False


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
        user.failed_login_count, user.locked_until, user.must_change_password = 0, None, False
        prt.used_at = utcnow()
        await _void_reset_tokens(db, user.id)
        await revoke_user_sessions(db, user)  # sign the user out everywhere
        await audit.record(db, action="auth.password_reset", actor=user, request=request)
        await db.commit()


# ---- changing a password while signed in, and administrator resets ----------------------
async def change_own_password(db: AsyncSession, user: User, session_id: UUID, current: str, new: str,
                              request: Request) -> None:
    """Needs the current password (a stolen access token alone can't take the account over).
    Other sessions are signed out; the one making the change stays signed in."""
    if not verify_password(user.password_hash, current):
        await _register_failure(db, user, request, "bad_password_on_change")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is incorrect")
    if verify_password(user.password_hash, new):
        raise HTTPException(422, "Choose a password different from your current one")
    user.password_hash = hash_password(new)
    user.must_change_password = False
    await _void_reset_tokens(db, user.id)
    await revoke_user_sessions(db, user, keep_session=session_id)
    await audit.record(db, action="user.password_change", actor=user, resource_type="user",
                       resource_id=user.id, request=request)
    await db.commit()


def check_reset_authority(actor: User, target: User) -> None:
    """Who may reset whose password. Tenant scoping (RLS + the caller's query) comes first; this
    adds the role rules: nobody resets themselves this way (they know their password, or use
    "forgot password"), and only a Super Admin can reset a Company Administrator."""
    if target.id == actor.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Use My account to change your own password")
    if target.role == Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super Admin passwords can't be reset here")
    if actor.role != Role.SUPER_ADMIN and target.role == Role.COMPANY_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Only a Super Admin can reset a Company Administrator's password")


async def verify_actor_password(db: AsyncSession, actor: User, password: str, request: Request, action: str) -> None:
    """Step-up check before a privileged account action: the administrator re-enters THEIR password."""
    if not verify_password(actor.password_hash, password):
        await _register_failure(db, actor, request, f"bad_password_on_{action}")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your password is incorrect")


async def admin_reset_password(db: AsyncSession, actor: User, target: User, *, mode: str,
                               temporary_password: str | None, request: Request) -> bool | None:
    """Administrator-initiated reset. Either way every existing session of the user ends and any
    older reset link stops working. Returns, for mode='email_link', whether the email was QUEUED.

    email_link          a single-use link (same tokens as "forgot password"), emailed to the user.
    temporary_password  the administrator sets a one-time password; the user must choose a new
                        one at their next sign-in. The password is never stored or logged in clear.
    """
    check_reset_authority(actor, target)
    if not target.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "Reactivate this account before resetting its password")
    await revoke_user_sessions(db, target)
    raw = None
    if mode == "temporary_password":
        assert temporary_password is not None
        target.password_hash = hash_password(temporary_password)
        target.must_change_password = True
        target.failed_login_count, target.locked_until = 0, None
        await _void_reset_tokens(db, target.id)
    else:
        raw = await issue_reset_token(db, target)
    await audit.record(db, action="user.password_reset_by_admin", actor=actor, company_id=target.company_id,
                       resource_type="user", resource_id=target.id, request=request,
                       meta={"mode": mode, "target_role": target.role.value})
    await db.commit()
    return queue_reset_email(target, raw) if raw else None


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