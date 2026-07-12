"""Per-user / per-handler rate limiting.

Attach a throttle to any handler::

    @router.command("roll")
    @throttle(3, per=60)                      # 3 calls per user per minute
    async def roll(update):
        ...

The default key is ``(handler, user id)``; pass ``key=`` to customize (e.g.
throttle per chat, or share one bucket across handlers). On limit the default
behaviour raises :class:`RateLimitedError` (friendly UX via the error layer);
pass ``on_limit=`` for custom behaviour (it may be a no-op to drop silently).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .exceptions import RateLimitedError
from .middleware import Event

_SPEC_ATTR = "__vitrine_throttle__"

KeyFn = Callable[[Event], str | None]
OnLimit = Callable[[Event, float], Awaitable[None]]


@dataclass(frozen=True)
class ThrottleSpec:
    limit: int
    per: float
    key: KeyFn | None = None
    on_limit: OnLimit | None = None


def throttle(
    limit: int,
    *,
    per: float = 60.0,
    key: KeyFn | None = None,
    on_limit: OnLimit | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a handler with a sliding-window throttle."""

    def mark(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, _SPEC_ATTR, ThrottleSpec(limit, per, key, on_limit))
        return fn

    return mark


def throttle_spec(fn: Callable[..., Any]) -> ThrottleSpec | None:
    return getattr(fn, _SPEC_ATTR, None)


class RateLimiter:
    """Sliding-window counters, one deque of timestamps per key."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._clock = clock

    def check(self, key: str, limit: int, per: float) -> float:
        """Record a hit; returns 0.0 if allowed, else seconds until a slot frees."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= per:
            hits.popleft()

        if len(hits) >= limit:
            return per - (now - hits[0])

        hits.append(now)
        return 0.0

    async def enforce(self, spec: ThrottleSpec, event: Event) -> None:
        key = spec.key(event) if spec.key else self._default_key(event)
        if key is None:
            return

        retry_after = self.check(key, spec.limit, spec.per)
        if retry_after <= 0:
            return

        if spec.on_limit is not None:
            await spec.on_limit(event, retry_after)
            raise _Drop()

        raise RateLimitedError(retry_after)

    @staticmethod
    def _default_key(event: Event) -> str | None:
        user = getattr(event.update, "effective_user", None)
        if user is None:
            return None

        return f"{event.handler_name}:{user.id}"


class _Drop(Exception):
    """Internal: handler skipped by a custom on_limit; swallowed by dispatch."""
