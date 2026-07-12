"""Middleware: the primary extension seam.

A middleware is ``async def mw(event, call_next)`` registered at bot or router
scope; it wraps every framework-handled update (commands, callbacks, messages,
and conversation steps alike)::

    @bot.middleware
    async def timing(event, call_next):
        started = time.monotonic()
        try:
            return await call_next(event)
        finally:
            metrics.observe(event.handler_name, time.monotonic() - started)

Anything placed in ``event.extras`` becomes injectable by name in the handler —
that is how an i18n middleware hands a translator to every view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

CallNext = Callable[["Event"], Awaitable[Any]]
Middleware = Callable[["Event", CallNext], Awaitable[Any]]


@dataclass
class Event:
    """What a middleware sees about the update being handled."""

    update: Any
    context: Any
    handler_name: str
    data: Any = None  # decoded callback data, if any
    state: Any = None  # conversation state, if inside a conversation
    extras: dict[str, Any] = field(default_factory=dict)


def compose(middlewares: list[Middleware], core: CallNext) -> CallNext:
    """Wrap ``core`` in the middlewares; the first listed runs outermost."""
    chain = core
    for mw in reversed(middlewares):
        chain = _wrap(mw, chain)

    return chain


def _wrap(mw: Middleware, nxt: CallNext) -> CallNext:
    async def runner(event: Event) -> Any:
        return await mw(event, nxt)

    return runner
