"""Installation state: does this system have a Super Admin yet, and how the first one is made.

Used by the CLI bootstrap (`python -m app.scripts.bootstrap`) and by the web setup endpoint, so
both paths share one implementation and one set of rules.
"""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import open_tenant_session
from app.core.permissions import Role
from app.core.security import hash_password
from app.modules.audit import service as audit
from app.modules.tenants.models import Company
from app.modules.users.models import User

PLATFORM_SLUG = "platform"


async def super_admin_count() -> int:
    async with open_tenant_session(None, bypass=True) as db:
        return await db.scalar(select(func.count()).select_from(User).where(
            User.role == Role.SUPER_ADMIN, User.is_active.is_(True))) or 0


async def ensure_platform(db: AsyncSession) -> Company:
    platform = await db.scalar(select(Company).where(Company.is_platform.is_(True)))
    if platform is None:
        platform = Company(name="MediRoutes Platform", slug=PLATFORM_SLUG, is_platform=True)
        db.add(platform)
        await db.flush()
    return platform


async def create_first_super_admin(email: str, password: str, full_name: str, *,
                                   via: str, require_empty: bool) -> User | None:
    """Create a Super Admin on the platform company. With `require_empty`, refuse (return None)
    if any active Super Admin already exists: the web setup can only ever run on a fresh system.
    A transaction-level advisory lock makes two simultaneous setup requests queue, not race."""
    async with open_tenant_session(None, bypass=True) as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('app:first-setup', 0))"))
        if require_empty and await db.scalar(select(func.count()).select_from(User).where(
                User.role == Role.SUPER_ADMIN, User.is_active.is_(True))):
            return None
        platform = await ensure_platform(db)
        if await db.scalar(select(User).where(User.company_id == platform.id,
                                              func.lower(User.email) == email.lower())):
            return None
        user = User(company_id=platform.id, email=email.lower(), full_name=full_name,
                    password_hash=hash_password(password), role=Role.SUPER_ADMIN)
        db.add(user)
        await db.flush()
        await audit.record(db, action="setup.super_admin_created", actor=user, resource_type="user",
                           resource_id=user.id, meta={"via": via})
        await db.commit()
        return user
