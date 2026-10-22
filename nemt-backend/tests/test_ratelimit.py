"""Rate limiting tests.

The general test suite disables rate limiting (see `hit()` in `app/core/ratelimit.py`)
because otherwise the auth fixtures would trip it on every test run. These tests turn
it back on temporarily to prove the limiter works.

Each test flips `settings.rate_limit_in_tests = True` via `monkeypatch`; pytest restores
the original value automatically at the end of each test, so subsequent tests in the
suite go back to bypassing the limiter.

Redis keys are cleared at the start of each test, so tests don't interfere with each
other even when run in sequence.
"""

import pytest

from app.core.config import settings
from app.core.ratelimit import hit


# ------------------------------------------------------------------ helpers


async def _clear_redis(pattern: str) -> None:
    """Delete every Redis key matching `pattern`. No-op if Redis is unavailable."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            async for k in r.scan_iter(pattern):
                await r.delete(k)
        finally:
            await r.aclose()
    except Exception:
        pass  # Redis down is fine; the limiter degrades open, so tests skip


# ------------------------------------------------------------------ unit


async def test_limiter_blocks_after_n_hits(monkeypatch):
    """A key with limit=3 allows 3 hits, rejects the 4th."""
    monkeypatch.setattr(settings, "env", "test")
    monkeypatch.setattr(settings, "rate_limit_in_tests", True)

    key = "test_rl_unique_key_abc123"
    await _clear_redis("test_rl_unique_key_abc123")

    # first three pass
    assert await hit(key, 3, 60) is True
    assert await hit(key, 3, 60) is True
    assert await hit(key, 3, 60) is True
    # fourth is rejected
    assert await hit(key, 3, 60) is False


# ------------------------------------------------------------------ integration


async def test_login_rate_limit_returns_429(client, super_token, monkeypatch):
    """Six rapid logins with the same email: first 5 get 401 (bad password),
    the 6th+ get 429 (rate limited)."""
    monkeypatch.setattr(settings, "env", "test")
    monkeypatch.setattr(settings, "rate_limit_in_tests", True)

    await _clear_redis("rl:login_*")

    body = {"email": "spray@example.com", "password": "wrong-wrong-wrong"}
    headers = {"X-Tenant": "platform"}

    statuses = []
    for _ in range(8):
        r = await client.post("/auth/login", headers=headers, json=body)
        statuses.append(r.status_code)

    # first 5 attempts: bad password → 401
    assert statuses[:5] == [401, 401, 401, 401, 401], statuses
    # 6th onward: rate limit trips → 429
    assert statuses[5:] == [429, 429, 429], statuses


async def test_register_rate_limit(client, super_token, monkeypatch):
    """5 registrations per IP per hour; the 6th is blocked."""
    monkeypatch.setattr(settings, "env", "test")
    monkeypatch.setattr(settings, "rate_limit_in_tests", True)

    await _clear_redis("rl:register_*")

    headers = {"X-Tenant": "platform"}
    statuses = []
    for i in range(7):
        r = await client.post("/auth/register", headers=headers, json={
            "email": f"rl_test_{i}@example.com",
            "password": "a-strong-password-1234",
            "full_name": f"RL Test {i}",
        })
        statuses.append(r.status_code)

    assert statuses[:5] == [201, 201, 201, 201, 201], statuses
    assert statuses[5:] == [429, 429], statuses