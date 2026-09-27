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
A router is frozen once a bot built from it: registering later would be
silently ignored, so it raises instead.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
)
from telegram.ext import filters as ptb_filters

from .callbacks import CallbackData, pattern_for
from .exceptions import ConfigurationError
from .middleware import Middleware
from .screens import ReplyButton

if TYPE_CHECKING:
    from .conversations import Conversation

F = TypeVar("F", bound=Callable[..., Any])

#: what Telegram accepts in a command name. PTB lowercases before validating,
#: so an invalid name would dispatch locally while ``setMyCommands`` rejects
#: the whole batch it appears in.
_COMMAND_NAME = re.compile(r"[a-z0-9_]{1,32}\Z")


def validate_command_name(name: str) -> str:
    if not _COMMAND_NAME.fullmatch(name):
        raise ConfigurationError(
            f"command name {name!r} would be rejected by Telegram: names are "
            f"1-32 characters of lowercase a-z, 0-9 and underscore. PTB would "
            f"still dispatch it, but publishing it would freeze every command "
            f"menu at whatever the previous run wrote."
        )

    return name


def split_docstring(fn: Callable[..., Any]) -> tuple[str, str]:
    """Split a handler docstring into (summary, detail body).

    The summary is the whole first paragraph, joined onto one line; the rest
    is the detail body shown by ``/help <command>``.
    """
    lines = inspect.cleandoc(fn.__doc__ or "").splitlines()
    end = next((i for i, line in enumerate(lines) if not line.strip()), len(lines))

    summary = " ".join(line.strip() for line in lines[:end])
    detail = "\n".join(lines[end:]).strip()

    return summary, detail


Kind = Literal["command", "callback", "message"]


@dataclass
class Registration:
    """One declaratively-registered handler and its metadata."""

    #: the transport: which PTB handler carries the update
    kind: Kind
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
    edits: bool = False  # opt in to receiving edited messages
    #: a reply-keyboard press: navigation, so it outranks a live conversation
    reply_button: bool = False
    middlewares: list[Middleware] = field(default_factory=list)


def _minus_edits(base: Any, edits: bool) -> Any:
    return base if edits else base & ~ptb_filters.UpdateType.EDITED


def ptb_handler(reg: Registration, callback: Callable[..., Any]) -> BaseHandler:
    """The one PTB handler a registration implies.

    Flat handlers and conversation steps both come from here. Edited messages
    are subtracted from what the handler would otherwise match unless
    ``edits=True``; commands keep PTB's default reach (``UpdateType.MESSAGES``),
    message handlers keep theirs (channel posts, business messages).
    """
    if reg.kind == "command":
        assert reg.command is not None
        return CommandHandler(
            reg.command,
            callback,
            filters=_minus_edits(ptb_filters.UpdateType.MESSAGES, reg.edits),
        )

    if reg.kind == "callback":
        assert reg.cb_model is not None
        # when= narrows inside the pattern, so a rejected press stays
        # available to a sibling handler on the same model
        return CallbackQueryHandler(
            callback, pattern=pattern_for(reg.cb_model, reg.cb_when)
        )

    base = (
        reg.filters if reg.filters is not None else ptb_filters.TEXT & ~ptb_filters.COMMAND
    )
    return MessageHandler(_minus_edits(base, reg.edits), callback)


class Router:
    def __init__(self, name: str | None = None) -> None:
        self.name = name
        self.registrations: list[Registration] = []
        self.middlewares: list[Middleware] = []
        self.children: list[Router] = []
        self.conversations: list[Conversation] = []
        self.raw_handlers: list[tuple[Any, int]] = []
        self.frozen = False

    def _check_mutable(self) -> None:
        if self.frozen:
            raise ConfigurationError(
                f"router {self.name or '(unnamed)'!r} belongs to a bot that is already "
                f"built; register handlers before calling build() or run()"
            )

    # -- registration decorators ----------------------------------------------

    def command(
        self,
        command: str | None = None,
        *,
        description: str | None = None,
        scope: str = "default",
        hidden: bool = False,
        edits: bool = False,
        group: int = 0,
    ) -> Callable[[F], F]:
        """Register a ``/command`` handler; ``Arg[...]`` params are its arguments.

        ``edits=True`` opts in to edited messages too: by default an edited
        ``/command`` is ignored, since editing an old command would re-run it.
        """
        self._check_mutable()

        def register(fn: F) -> F:
            desc = split_docstring(fn)[0] if description is None else description
            self.registrations.append(
                Registration(
                    kind="command",
                    fn=fn,
                    name=fn.__name__,
                    command=validate_command_name(command or fn.__name__),
                    description=desc,
                    scope=scope,
                    hidden=hidden,
                    edits=edits,
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
    ) -> Callable[[F], F]:
        """Register a handler for one typed callback-data model.

        The decoded, validated instance is injected as ``data``. ``when``
        narrows the match on the decoded payload; a press it rejects stays
        available to another handler on the same model.

        Keep callback handlers in the default group: a catch-all sits last
        there to answer presses nothing took, Telegram accepts one answer per
        press, and a handler in a later group would answer too late.
        """
        self._check_mutable()

        def register(fn: F) -> F:
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
        self, filters: Any = None, *, edits: bool = False, group: int = 0
    ) -> Callable[[F], F]:
        """Register a message handler with PTB filters (defaults to text messages).

        Edited messages are excluded unless ``edits=True``. While a caller has
        a live conversation, its steps see their messages first.
        """
        self._check_mutable()
        return self._message(filters, edits=edits, group=group, reply_button=False)

    def reply_button(self, *buttons: ReplyButton | str, group: int = 0) -> Callable[[F], F]:
        """Register a handler for presses of one or more reply-keyboard buttons.

        A press arrives as a text message equal to the label, so this is a
        message handler with an exact-text filter. Presses are navigation:
        they reach this handler even while the caller is inside a
        conversation whose step would accept any text.
        """
        self._check_mutable()
        labels = [b.text if isinstance(b, ReplyButton) else b for b in buttons]
        if not labels:
            raise ValueError("reply_button needs at least one button or label")

        return self._message(
            ptb_filters.Text(labels), edits=False, group=group, reply_button=True
        )

    def _message(
        self, filters: Any, *, edits: bool, group: int, reply_button: bool
    ) -> Callable[[F], F]:
        def register(fn: F) -> F:
            self.registrations.append(
                Registration(
                    kind="message",
                    fn=fn,
                    name=fn.__name__,
                    filters=filters,
                    edits=edits,
                    group=group,
                    reply_button=reply_button,
                )
            )
            return fn

        return register

    def middleware(self, mw: Middleware) -> Middleware:
        """Attach middleware to every handler on this router and its children."""
        self._check_mutable()
        self.middlewares.append(mw)
        return mw

    def include(self, router: Router) -> None:
        """Mount a sub-router."""
        self._check_mutable()
        self.children.append(router)

    def conversation(self, conversation: Conversation) -> Conversation:
        """Mount a guided conversation on this router."""
        self._check_mutable()
        self.conversations.append(conversation)
        return conversation

    def raw(self, handler: BaseHandler, group: int = 0) -> BaseHandler:
        """Escape hatch: register a plain PTB handler untouched."""
        self._check_mutable()
        self.raw_handlers.append((handler, group))
        return handler

    # -- traversal -------------------------------------------------------------

    def freeze(self) -> None:
        """Refuse further registrations here and in every sub-router."""
        self.frozen = True
        for child in self.children:
            child.freeze()
        for conv in self.conversations:
            conv.frozen = True

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
    ) -> Iterator[tuple[Conversation, list[Middleware]]]:
        chain = [*(outer_middlewares or []), *self.middlewares]
        for conv in self.conversations:
            yield conv, chain

        for child in self.children:
            yield from child.walk_conversations(chain)

    def walk_raw(self) -> Iterator[tuple[Any, int]]:
        yield from self.raw_handlers
        for child in self.children:
            yield from child.walk_raw()
