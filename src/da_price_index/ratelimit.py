"""Per-client-IP request rate limiting for the HTTP API.

Fixed-window counters (per minute / hour / day) held in process memory. This is
correct for a single API instance; running several replicas would give each its
own counters, so the effective limit would multiply by the replica count.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Window:
    """A rate-limit window: at most `limit` requests per `seconds`."""

    name: str
    seconds: int
    limit: int


class RateLimiter:
    """Fixed-window request counter keyed by client identifier."""

    _PRUNE_INTERVAL_SECONDS = 60

    def __init__(self, windows: Iterable[Window], clock: Callable[[], float] = time.time) -> None:
        self.windows = [w for w in windows if w.limit > 0]
        self._clock = clock
        # (client_key, window_name) -> (window_start, request_count)
        self._counts: dict[tuple[str, str], tuple[int, int]] = {}
        self._window_seconds = {w.name: w.seconds for w in self.windows}
        self._last_prune = clock()

    def hit(self, key: str) -> tuple[bool, int]:
        """Record one request for `key`.

        Returns (allowed, retry_after_seconds). Rejected requests are not
        counted, so a client that backs off regains access when the window rolls.
        """
        now = self._clock()
        self._prune(now)

        pending: list[tuple[Window, int, int]] = []
        for window in self.windows:
            start = int(now // window.seconds) * window.seconds
            stored = self._counts.get((key, window.name))
            count = stored[1] if stored is not None and stored[0] == start else 0
            if count >= window.limit:
                return False, max(1, math.ceil(start + window.seconds - now))
            pending.append((window, start, count))

        for window, start, count in pending:
            self._counts[(key, window.name)] = (start, count + 1)
        return True, 0

    def _prune(self, now: float) -> None:
        """Drop counters whose window has ended so memory stays bounded."""
        if now - self._last_prune < self._PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        expired = [
            k for k, (start, _) in self._counts.items()
            if start + self._window_seconds[k[1]] <= now
        ]
        for k in expired:
            del self._counts[k]


class RateLimitMiddleware:
    """ASGI middleware that answers 429 once a client IP exceeds any window."""

    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        ip_header: str = "x-real-ip",
        exempt_paths: Iterable[str] = ("/health",),
        exempt_prefixes: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.ip_header = ip_header.lower().encode("latin-1")
        self.exempt_paths = frozenset(exempt_paths)
        self.exempt_prefixes = tuple(exempt_prefixes)

    def _client_ip(self, scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name == self.ip_header:
                ip = value.decode("latin-1").split(",")[0].strip()
                if ip:
                    return ip
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] in self.exempt_paths
            or scope["path"].startswith(self.exempt_prefixes)
        ):
            await self.app(scope, receive, send)
            return

        allowed, retry_after = self.limiter.hit(self._client_ip(scope))
        if not allowed:
            response = JSONResponse(
                {"detail": "Rate limit exceeded. Try again later."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def limiter_from_settings(per_minute: int, per_hour: int, per_day: int) -> RateLimiter:
    """Build the standard minute/hour/day limiter."""
    return RateLimiter([
        Window("minute", 60, per_minute),
        Window("hour", 3600, per_hour),
        Window("day", 86400, per_day),
    ])
