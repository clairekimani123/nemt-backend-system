"""Create the platform company + a Super Admin (first install, or after a system reset).

    python -m app.scripts.bootstrap --email you@example.com

Idempotent: re-running with an email that already exists changes nothing.
"""

import argparse
import asyncio
import getpass

from app.core.db import engine
from app.modules.setup.service import create_first_super_admin


async def main(email: str, password: str, name: str) -> None:
    user = await create_first_super_admin(email, password, name, via="cli", require_empty=False)
    if user is None:
        print("Super admin already exists.")
    else:
        print(f"Created super admin {email} (log in with X-Tenant: platform)")
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="Platform Admin")
    ap.add_argument("--password")
    a = ap.parse_args()
    pw = a.password or getpass.getpass("Password (12+ chars): ")
    if len(pw) < 12:
        raise SystemExit("Password must be at least 12 characters.")
    asyncio.run(main(a.email, pw, a.name))
