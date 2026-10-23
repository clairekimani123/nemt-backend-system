"""Rate limiting via Redis.

A small sliding-window limiter. Keyed by (endpoint, ip) or (endpoint, email). Uses Redis
INCR + EXPIRE, which is atomic enough for this purpose.

If Redis is down, requests are allowed. The system degrades open: rate limiting is a
protection, not a gate. An attacker with direct Redis access has bigger problems than
brute-forcing your login form.

Tests: disabled by default (``env == "test"`` bypasses). A dedicated test flips
``rate_limit_in_tests = True`` to verify the mechanism works.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.core.config import settings

log = logging.getLogger(__name__)


async def _redis():
    """Build a short-lived async Redis client."""
    import redis.asyncio as aioredis
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def hit(key: str, limit: int, window_seconds: int) -> bool:
    """Return True if under the limit, False if over. Increments the counter atomically.

    The Redis key gets a TTL on the first INCR, so repeated calls within
    `window_seconds` share one counter that expires cleanly.
    """
    # Tests run hundreds of logins in seconds; the limiter would block the suite.
    # Set rate_limit_in_tests=True in a test to actually exercise the limiter.
    if settings.env == "test" and not settings.rate_limit_in_tests:
        return True

    try:
        r = await _redis()
        try:
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            value, _ = await pipe.execute()
            return int(value) <= limit
        finally:
            await r.aclose()
    except Exception as exc:                     # Redis down → allow
        log.warning("ratelimit unavailable: %s", exc)
        return True


def limiter(
    name: str,
    limit: int,
    window_seconds: int,
    key_fn: Callable[[Request], str] | None = None,
):
    """Return a FastAPI dependency that enforces `limit` per `window_seconds`.

    Default key is (name, ip). Override `key_fn` to add request-specific data
    (e.g. email) to the key for tighter limits.
    """

    async def _dep(request: Request) -> None:
        suffix = key_fn(request) if key_fn else (
            request.client.host if request.client else "?"
        )
        key = f"rl:{name}:{hashlib.sha256(suffix.encode()).hexdigest()[:16]}"
        if not await hit(key, limit, window_seconds):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests. Please try again later.",
                headers={"Retry-After": str(window_seconds)},
            )

    return _dep


# pre-built limiters for the auth endpoints
login_by_ip = limiter("login_ip", 20, 60)
register_by_ip = limiter("register_ip", 5, 3600)
forgot_by_ip = limiter("forgot_ip", 3, 3600)
mfa_by_ip = limiter("mfa_ip", 10, 60)