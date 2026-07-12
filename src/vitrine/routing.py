"""Declarative registration: routers, commands, callbacks, messages.

A :class:`Router` collects handlers declaratively and composes into trees::

    router = Router()

    @router.command("start", description="Open the main menu")
    async def start(update, context): ...

    @router.callback(MenuCB)
    async def on_menu(data: MenuCB, user): ...

    admin = Router()
    router.include(admin)
    bot.include(router)

Routers carry their own middleware (applied to every handler registered on
them and their children) and are how a large app splits its bot layer into
packages. Raw PTB handlers remain a first-class escape hatch via ``.raw()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Iterator

from .callbacks import CallbackData
from .middleware import Middleware

if TYPE_CHECKING:
    from .conversations import Conversation


@dataclass
class Registration:
    """One declaratively-registered handler and its metadata."""

    kind: str  # "command" | "callback" | "message"
    fn: Callable[..., Any]
    name: str
    command: str | None = None
    description: str = ""
    scope: str = "default"  # command-menu scope ("default", "admin", ...)
    hidden: bool = False  # exclude from /help and command menus
    cb_model: type[CallbackData] | None = None
    cb_when: Callable[[CallbackData], bool] | None = None
    filters: Any = None  # PTB filters for message handlers
    group: int = 0
    middlewares: list[Middleware] = field(default_factory=list)


class Router:
    def __init__(self, name: str | None = None) -> None:
        self.name = name
        self.registrations: list[Registration] = []
        self.middlewares: list[Middleware] = []
        self.children: list["Router"] = []
        self.conversations: list["Conversation"] = []
        self.raw_handlers: list[tuple[Any, int]] = []

    # -- registration decorators ----------------------------------------------

    def command(
        self,
        command: str | None = None,
        *,
        description: str | None = None,
        scope: str = "default",
        hidden: bool = False,
        group: int = 0,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a ``/command`` handler; extra params become typed arguments."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            desc = description
            if desc is None:
                desc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""

            self.registrations.append(
                Registration(
                    kind="command",
                    fn=fn,
                    name=fn.__name__,
                    command=command or fn.__name__,
                    description=desc,
                    scope=scope,
                    hidden=hidden,
                    group=group,
                )
            )

            return fn

        return register

    def callback(
        self,
        model: type[CallbackData],
        *,
        when: Callable[[Any], bool] | None = None,
        group: int = 0,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a handler for one typed callback-data model.

        The decoded, validated instance is injected as the ``data`` parameter.
        ``when`` optionally narrows the match on the decoded payload.
        """

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.registrations.append(
                Registration(
                    kind="callback",
                    fn=fn,
                    name=fn.__name__,
                    cb_model=model,
                    cb_when=when,
                    group=group,
                )
            )

            return fn

        return register

    def message(
        self, filters: Any = None, *, group: int = 0
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a message handler with PTB filters (defaults to text messages)."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.registrations.append(
                Registration(
                    kind="message", fn=fn, name=fn.__name__, filters=filters, group=group
                )
            )

            return fn

        return register

    def middleware(self, mw: Middleware) -> Middleware:
        """Attach middleware to every handler on this router and its children."""
        self.middlewares.append(mw)
        return mw

    def include(self, router: "Router") -> None:
        """Mount a sub-router."""
        self.children.append(router)

    def conversation(self, conversation: "Conversation") -> "Conversation":
        """Mount a guided conversation on this router."""
        self.conversations.append(conversation)
        return conversation

    def raw(self, handler: Any, group: int = 0) -> Any:
        """Escape hatch: register a plain PTB handler untouched."""
        self.raw_handlers.append((handler, group))
        return handler

    # -- traversal -------------------------------------------------------------

    def walk(
        self, outer_middlewares: list[Middleware] | None = None
    ) -> Iterator[Registration]:
        """Yield all registrations with accumulated middleware chains."""
        chain = [*(outer_middlewares or []), *self.middlewares]
        for reg in self.registrations:
            yield replace(reg, middlewares=chain)

        for child in self.children:
            yield from child.walk(chain)

    def walk_conversations(
        self, outer_middlewares: list[Middleware] | None = None
    ) -> Iterator[tuple["Conversation", list[Middleware]]]:
        chain = [*(outer_middlewares or []), *self.middlewares]
        for conv in self.conversations:
            yield conv, chain

        for child in self.children:
            yield from child.walk_conversations(chain)

    def walk_raw(self) -> Iterator[tuple[Any, int]]:
        yield from self.raw_handlers
        for child in self.children:
            yield from child.walk_raw()
