import os
import subprocess
import sys
import tempfile

from cryptography.fernet import Fernet

# Must be set BEFORE the app is imported (settings are read at import time).
os.environ["ENV"] = "test"
os.environ["MAIL_BACKEND"] = "memory"
os.environ["CELERY_ALWAYS_EAGER"] = "true"
os.environ.setdefault("BLIND_INDEX_KEY", "test-blind-index-key-test-blind-index-key")
os.environ["GEOCODER_BACKEND"] = "fake"
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="nemt-test-storage-")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-123")
os.environ.setdefault("FIELD_ENCRYPTION_KEYS", Fernet.generate_key().decode())
# Tests ALWAYS use the dedicated *_test database (they TRUNCATE tables). Never the dev DB.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://nemt_app:app_dev_pw@localhost:5432/nemt_test")
os.environ["DATABASE_OWNER_URL"] = os.environ.get(
    "TEST_DATABASE_OWNER_URL", "postgresql+asyncpg://nemt_owner:owner_dev_pw@localhost:5432/nemt_test")
assert os.environ["DATABASE_URL"].endswith("_test") and os.environ["DATABASE_OWNER_URL"].endswith("_test"), \
    "refusing to run tests against a non-test database"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

# Tests only: cheap argon2 parameters so the suite is fast. Production keeps the strong defaults.
import app.core.security as _security  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.mail import MemoryMailer  # noqa: E402
from app.main import app  # noqa: E402
from app.scripts.bootstrap import main as bootstrap_super_admin  # noqa: E402
from tests.helpers import PW, login  # noqa: E402

_security._ph = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    eng = create_async_engine(settings.database_owner_url, poolclass=NullPool)
    async with eng.begin() as conn:
        names = (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            " AND tablename NOT IN ('alembic_version', 'spatial_ref_sys')"))).scalars().all()
        await conn.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"))
    await eng.dispose()
    MemoryMailer.outbox.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def super_token(client) -> str:
    await bootstrap_super_admin("root@platform.example.com", PW, "Root")
    return (await login(client, "platform", "root@platform.example.com"))["access_token"]