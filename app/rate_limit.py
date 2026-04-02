from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

# Per-path rate limits: (max_requests, window_seconds)
_PATH_LIMITS: dict[str, tuple[int, int]] = {
    "/upload": (10, 60),
    "/ws": (20, 60),
    "/logs/export": (3, 60),
    "/governance/export": (3, 60),
    "/support/export": (2, 60),
    "/metrics": (30, 60),
    "/api/license/import": (5, 300),
}

_DEFAULT_LIMIT = (60, 60)  # 60 requests per minute for other endpoints
_SENSITIVE_GET_PATHS = frozenset({
    "/health",
    "/metrics",
    "/api/readiness",
    "/api/license",
    "/logs/export",
    "/governance/export",
    "/support/export",
})
_EVICTION_INTERVAL = 256


class _RateBucket:
    __slots__ = ("timestamps", "lock", "last_seen")

    def __init__(self):
        self.timestamps: list[float] = []
        self.lock = Lock()
        self.last_seen = 0.0

    def check(self, max_requests: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window
        with self.lock:
            self.timestamps = [ts for ts in self.timestamps if ts > cutoff]
            self.last_seen = now
            if len(self.timestamps) >= max_requests:
                oldest = self.timestamps[0] if self.timestamps else now
                retry_after = int(oldest + window - now) + 1
                return False, retry_after
            self.timestamps.append(now)
            return True, 0


_buckets: dict[str, _RateBucket] = defaultdict(_RateBucket)

# Exempt paths that should never be rate-limited
_EXEMPT_PATHS = frozenset({
    "/health", "/health/live", "/health/ready", "/api/version",
    "/", "/dashboard",
})
_bucket_housekeeping_lock = Lock()
_bucket_uses = 0


def _prune_buckets(now: float) -> None:
    expired = []
    for key, bucket in list(_buckets.items()):
        if now - bucket.last_seen > 600:
            expired.append(key)
    for key in expired:
        _buckets.pop(key, None)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip rate limiting for exempt paths and static assets
        if path in _EXEMPT_PATHS or path.startswith("/static/"):
            return await call_next(request)

        if request.method not in {"POST", "PUT", "DELETE", "PATCH"} and path not in _SENSITIVE_GET_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        bucket_key = f"{client_ip}:{path}"
        max_requests, window = _PATH_LIMITS.get(path, _DEFAULT_LIMIT)
        bucket = _buckets[bucket_key]
        allowed, retry_after = bucket.check(max_requests, window)

        if not allowed:
            return Response(
                content=f'{{"detail":"Rate limit exceeded. Try again in {retry_after}s."}}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

        global _bucket_uses
        _bucket_uses += 1
        if _bucket_uses % _EVICTION_INTERVAL == 0:
            with _bucket_housekeeping_lock:
                _prune_buckets(time.monotonic())

        return await call_next(request)
