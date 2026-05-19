"""In-memory token-bucket rate limiter.

v1 has no Redis dependency. Process-local only — if you horizontally scale
beyond 1 uvicorn worker, swap this for a Redis-backed implementation.

Limits per spec §12:
- 20 generations/user/hour
- 200 generations/org/day
- $50/month org cost cap

Raises FastAPI HTTPException with the appropriate retry headers on overflow.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import HTTPException


PER_USER_PER_HOUR = 20
PER_ORG_PER_DAY = 200
ORG_MONTHLY_COST_CAP_USD = 50.0


@dataclass
class _Bucket:
    """Sliding-window counter; we track event timestamps in a deque."""

    events: deque = field(default_factory=deque)

    def prune(self, window_seconds: float) -> None:
        now = time.time()
        cutoff = now - window_seconds
        while self.events and self.events[0] < cutoff:
            self.events.popleft()

    def count(self, window_seconds: float) -> int:
        self.prune(window_seconds)
        return len(self.events)

    def add(self) -> None:
        self.events.append(time.time())


class RateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._user_buckets: dict[str, _Bucket] = {}
        self._org_bucket = _Bucket()
        self._org_month_cost_usd: float = 0.0
        self._org_month_started: float = time.time()

    async def reset(self) -> None:
        async with self._lock:
            self._user_buckets.clear()
            self._org_bucket = _Bucket()
            self._org_month_cost_usd = 0.0
            self._org_month_started = time.time()

    def _maybe_roll_month(self) -> None:
        # Roll the monthly cost counter every 30 days
        if time.time() - self._org_month_started > 30 * 24 * 3600:
            self._org_month_cost_usd = 0.0
            self._org_month_started = time.time()

    async def check(self, user_key: str) -> None:
        """Raise HTTPException if the request should be blocked.

        Reserves a slot in both user and org buckets on success.
        """
        async with self._lock:
            self._maybe_roll_month()

            # Per-user — 20/hour
            ub = self._user_buckets.setdefault(user_key, _Bucket())
            if ub.count(3600) >= PER_USER_PER_HOUR:
                # When's the earliest event going to drop out?
                oldest = ub.events[0]
                retry = max(1, int((oldest + 3600) - time.time()))
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limited",
                        "scope": "user",
                        "limit": PER_USER_PER_HOUR,
                        "retry_after_seconds": retry,
                    },
                    headers={"Retry-After": str(retry)},
                )

            # Per-org — 200/day
            if self._org_bucket.count(24 * 3600) >= PER_ORG_PER_DAY:
                oldest = self._org_bucket.events[0]
                retry = max(1, int((oldest + 24 * 3600) - time.time()))
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limited",
                        "scope": "org",
                        "limit": PER_ORG_PER_DAY,
                        "retry_after_seconds": retry,
                    },
                    headers={"Retry-After": str(retry)},
                )

            # Monthly cost cap
            if self._org_month_cost_usd >= ORG_MONTHLY_COST_CAP_USD:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "monthly_cap_exceeded",
                        "cap_usd": ORG_MONTHLY_COST_CAP_USD,
                        "spent_usd": round(self._org_month_cost_usd, 4),
                    },
                )

            ub.add()
            self._org_bucket.add()

    async def record_cost(self, cost_usd: float) -> None:
        async with self._lock:
            self._maybe_roll_month()
            self._org_month_cost_usd += max(0.0, float(cost_usd or 0.0))

    async def stats(self, user_key: str) -> dict:
        async with self._lock:
            ub = self._user_buckets.get(user_key)
            return {
                "user_used_last_hour": ub.count(3600) if ub else 0,
                "user_limit_per_hour": PER_USER_PER_HOUR,
                "org_used_today": self._org_bucket.count(24 * 3600),
                "org_limit_per_day": PER_ORG_PER_DAY,
                "org_month_cost_usd": round(self._org_month_cost_usd, 4),
                "org_month_cost_cap_usd": ORG_MONTHLY_COST_CAP_USD,
            }


# Module-level singleton
_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
