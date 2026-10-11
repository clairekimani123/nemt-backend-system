import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app import models  # noqa: F401  (registers all tables)
from app.core.base import Base
from app.core.config import settings

target_metadata = Base.metadata


# PostGIS owns these tables; Alembic must never touch them.
POSTGIS_TABLES = {"spatial_ref_sys", "geometry_columns", "geography_columns"}


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    return not (type_ == "table" and name in POSTGIS_TABLES)


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    # Migrations run as the OWNER role; the app runs as a different, RLS-bound role.
    engine = create_async_engine(settings.database_owner_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline mode not supported")
asyncio.run(run_async())