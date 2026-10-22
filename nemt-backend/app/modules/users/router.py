from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, require_permission
from app.core.permissions import Permission, Role
from app.core.security import hash_password, utcnow
from app.modules.audit import service as audit
from app.modules.auth.models import RefreshToken
from app.modules.auth.service import get_user_by_email
from app.modules.users.models import User
from app.modules.users.schemas import UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.get("", response_model=list[UserOut])
async def list_users(
    role: Role | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    actor: User = Depends(require_permission(Permission.USERS_READ)),
    db: AsyncSession = Depends(get_db),
):
    q = select(User).where(User.company_id == actor.company_id)  # app-layer filter + RLS behind it
    if role:
        q = q.where(User.role == role)
    return (await db.scalars(q.order_by(User.created_at.desc(), User.id).limit(limit).offset(offset))).all()


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
    if target.id == actor.id and (
        changes.get("is_active") is False or ("role" in changes and changes["role"] != actor.role)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot demote or deactivate yourself")
    for field, value in changes.items():
        setattr(target, field, value)
    if changes.get("is_active") is False:  # sign them out everywhere
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == target.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
    await audit.record(db, action="user.update", actor=actor, resource_type="user",
                       resource_id=target.id, request=request, meta={"fields": sorted(changes)})
    await db.commit()
    return target


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