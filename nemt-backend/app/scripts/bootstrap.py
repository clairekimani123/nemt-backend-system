"""Create the platform company + first Super Admin.

    python -m app.scripts.bootstrap --email you@example.com
"""

import argparse
import asyncio
import getpass

from sqlalchemy import select

from app.core.db import engine, open_tenant_session
from app.core.permissions import Role
from app.core.security import hash_password
from app.modules.tenants.models import Company
from app.modules.users.models import User


async def main(email: str, password: str, name: str) -> None:
    async with open_tenant_session(None, bypass=True) as db:
        platform = await db.scalar(select(Company).where(Company.is_platform.is_(True)))
        if platform is None:
            platform = Company(name="MediRoutes Platform", slug="platform", is_platform=True)
            db.add(platform)
            await db.flush()
        exists = await db.scalar(select(User).where(User.company_id == platform.id, User.email == email))
        if exists:
            print("Super admin already exists.")
        else:
            db.add(User(company_id=platform.id, email=email.lower(), full_name=name,
                        password_hash=hash_password(password), role=Role.SUPER_ADMIN))
            print(f"Created super admin {email} (log in with X-Tenant: platform)")
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Platform Admin")
    ap.add_argument("--password")
    a = ap.parse_args()
    asyncio.run(main(a.email, a.password or getpass.getpass("Password (12+ chars): "), a.name))