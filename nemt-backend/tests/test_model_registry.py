import subprocess
import sys

from sqlalchemy import text

from app.core.db import open_tenant_session


async def test_app_models_registers_every_table():
    """`alembic revision --autogenerate` only knows the tables listed in app/models.py. If a model is
    missing there, the next migration would try to DROP that table. We check in a FRESH process,
    importing nothing but app.models (in this process the routers would already have loaded
    everything and hidden the mistake)."""
    out = subprocess.run(
        [sys.executable, "-c", "import app.models; from app.core.base import Base; print(','.join(sorted(Base.metadata.tables)))"],
        capture_output=True, text=True, check=True).stdout.strip()
    registered = set(out.split(","))
    async with open_tenant_session(None, bypass=True) as s:
        in_db = set((await s.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            " AND tablename NOT IN ('alembic_version', 'spatial_ref_sys')"))).scalars())
    assert registered == in_db, f"missing from app/models.py: {sorted(in_db - registered)}; extra: {sorted(registered - in_db)}"