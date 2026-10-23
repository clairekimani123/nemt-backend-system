from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud import contains_pattern
from app.core.deps import Principal, get_current_user, get_db, get_principal, require_permission
from app.core.permissions import Permission, Role
from app.core.security import hash_password
from app.core.storage import Storage, get_storage
from app.modules.audit import service as audit
from app.modules.auth import service as auth
from app.modules.auth.service import get_user_by_email
from app.modules.users import avatar
from app.modules.users.models import User
from app.modules.users.schemas import (
    AdminPasswordResetIn,
    AdminPasswordResetOut,
    PasswordChangeIn,
    StepUpIn,
    UserCreate,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/users", tags=["users"])
# These roles need a matching profile row (drivers / patients), so a plain role switch would
# leave a login that dispatch can't use. Drivers are created via POST /drivers.
PROFILE_ROLES = {Role.DRIVER, Role.PASSENGER}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    body: PasswordChangeIn, request: Request, principal: Principal = Depends(get_principal),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Any signed-in user changes their own password (also how a temporary password is replaced).
    This session stays signed in; every other session of the account is signed out."""
    await auth.change_own_password(db, user, principal.session_id, body.current_password, body.new_password, request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/avatar")
async def my_avatar(user: User = Depends(get_current_user), storage: Storage = Depends(get_storage)):
    return await avatar.serve(storage, user)


@router.put("/me/avatar", response_model=UserOut)
async def upload_my_avatar(
    request: Request, file: UploadFile = File(...),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
):
    """Every account type sets its OWN photo; there is no way to target another user here."""
    data, kind = await avatar.read_image(file)
    old = await avatar.replace(storage, user, data, kind)
    try:
        await audit.record(db, action="user.avatar_update", actor=user, resource_type="user",
                           resource_id=user.id, request=request, meta={"content_type": kind, "size": len(data)})
        await db.commit()
    except Exception:
        await storage.delete(user.avatar_key)  # type: ignore[arg-type]  # no orphan file
        raise
    if old:
        await storage.delete(old)
    await db.refresh(user)
    return user


@router.delete("/me/avatar", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_avatar(
    request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
):
    old = avatar.clear(user)
    await audit.record(db, action="user.avatar_delete", actor=user, resource_type="user",
                       resource_id=user.id, request=request)
    await db.commit()
    if old:
        await storage.delete(old)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("", response_model=list[UserOut])
async def list_users(
    role: Role | None = None,
    q: str | None = Query(None, max_length=100, description="Search name or email"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(require_permission(Permission.USERS_READ)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).where(User.company_id == actor.company_id)  # app-layer filter + RLS behind it
    if role:
        stmt = stmt.where(User.role == role)
    if q and q.strip():
        like = contains_pattern(q)
        stmt = stmt.where(or_(User.full_name.ilike(like), User.email.ilike(like)))
    return (await db.scalars(stmt.order_by(User.created_at.desc(), User.id).limit(limit).offset(offset))).all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate, request: Request,
    actor: User = Depends(require_permission(Permission.USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    if body.role == Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot assign this role")
    if await get_user_by_email(db, actor.company_id, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use")
    if body.role == Role.DRIVER:
        raise HTTPException(422, "Create drivers with POST /drivers so they get a driver profile")
    user = User(
        company_id=actor.company_id, email=body.email, password_hash=hash_password(body.password),
        full_name=body.full_name, phone=body.phone, role=body.role,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use") from None
    await audit.record(db, action="user.create", actor=actor, resource_type="user",
                       resource_id=user.id, request=request, meta={"role": body.role.value})
    await db.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: UUID, body: UserUpdate, request: Request,
    actor: User = Depends(require_permission(Permission.USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    target = await db.scalar(select(User).where(User.id == user_id, User.company_id == actor.company_id))
    if target is None:  # also what a user from another company gets: existence is not revealed
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("role") == Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot assign this role")
    if "role" in changes and changes["role"] != target.role and (
        changes["role"] in PROFILE_ROLES or target.role in PROFILE_ROLES
    ):
        raise HTTPException(422, "Driver and passenger accounts can't be switched to or from another role")
    if target.id == actor.id and (
        changes.get("is_active") is False or ("role" in changes and changes["role"] != actor.role)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot demote or deactivate yourself")
    for field, value in changes.items():
        setattr(target, field, value)
    if changes.get("is_active") is False:  # sign them out everywhere
        await auth.revoke_user_sessions(db, target)
    await audit.record(db, action="user.update", actor=actor, resource_type="user",
                       resource_id=target.id, request=request, meta={"fields": sorted(changes)})
    await db.commit()
    return target

async def _company_user(db: AsyncSession, actor: User, user_id: UUID) -> User:
    target = await db.scalar(select(User).where(User.id == user_id, User.company_id == actor.company_id))
    if target is None:  # another company's user is indistinguishable from a missing one
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return target


@router.post("/{user_id}/password-reset", response_model=AdminPasswordResetOut)
async def reset_user_password(
    user_id: UUID, body: AdminPasswordResetIn, request: Request,
    actor: User = Depends(require_permission(Permission.USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """A Company Administrator helps a user in THEIR company who forgot their password. The
    administrator never sees the old password; see auth.service.admin_reset_password."""
    target = await _company_user(db, actor, user_id)
    auth.check_reset_authority(actor, target)
    await auth.verify_actor_password(db, actor, body.admin_password, request, "password_reset")
    queued = await auth.admin_reset_password(db, actor, target, mode=body.mode,
                                             temporary_password=body.temporary_password, request=request)
    return AdminPasswordResetOut(mode=body.mode, email_queued=queued,
                                 must_change_password=body.mode == "temporary_password")


@router.post("/{user_id}/sessions/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_user_sessions(
    user_id: UUID, body: StepUpIn, request: Request,
    actor: User = Depends(require_permission(Permission.USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Sign a user out on every device (e.g. a lost phone)."""
    target = await _company_user(db, actor, user_id)
    auth.check_reset_authority(actor, target)
    await auth.verify_actor_password(db, actor, body.admin_password, request, "revoke_sessions")
    await auth.revoke_user_sessions(db, target)
    await audit.record(db, action="user.sessions_revoked", actor=actor, resource_type="user",
                       resource_id=target.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

@router.post("/users/{user_id}/force-logout", status_code=status.HTTP_204_NO_CONTENT)
async def force_logout(user_id: UUID, request: Request,
                       actor: User = Depends(require_permission(Permission.USERS_WRITE)),
                       db: AsyncSession = Depends(get_db)):
    """Revoke every refresh token for this user. All devices must log in again."""
    target = await db.scalar(select(User).where(User.id == user_id,
                                               User.company_id == actor.company_id))
    if target is None:
        raise HTTPException(404, "User not found")
    if target.id == actor.id:
        raise HTTPException(400, "Use /auth/logout to log yourself out")
    from app.core.security import utcnow
    from app.modules.auth.models import RefreshToken
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == target.id,
               RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await audit.record(db, action="user.force_logout", actor=actor,
                       resource_type="user", resource_id=target.id, request=request)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
