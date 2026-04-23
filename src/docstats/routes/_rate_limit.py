"""Simple in-memory rate limiter for public-facing routes (Phase 9.B).

Used by ``routes/share.py`` to guard the share-token 2FA endpoint against
brute-force attacks.  State lives in the process — resets on restart.
A distributed alternative (Redis) is a Phase 10+ upgrade path.

Usage::

    from docstats.routes._rate_limit import RateLimiter
    _2fa_limiter = RateLimiter(max_attempts=10, window_seconds=900)

    @router.post("/share/{token}/verify")
    def verify(request: Request):
        ip = request.client.host or "unknown"
        if not _2fa_limiter.allow(ip):
            raise HTTPException(429, detail="Too many attempts — try again later.")
        ...
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock


class RateLimiter:
    """Sliding-window counter keyed by a string (typically an IP address)."""

    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """Return True if the request is within the limit, False otherwise.

        Records the current attempt regardless — callers should check the
        return value and reject on False.
        """
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets[key]
            # Evict stale entries
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True

    def remaining(self, key: str) -> int:
        """Return attempts remaining in the current window (read-only)."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._buckets[key]
            count = sum(1 for t in bucket if t >= cutoff)
            return max(0, self._max - count)
