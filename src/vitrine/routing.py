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

import inspect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from telegram.ext import (
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

#: what Telegram accepts in a command name. PTB lowercases before validating,
#: so an invalid name dispatches fine locally while ``setMyCommands`` rejects
#: the whole batch it appears in -- hence the check happens here, at
#: registration, where the offending decorator is on the stack.
_COMMAND_NAME = re.compile(r"[a-z0-9_]{1,32}\Z")


def validate_command_name(name: str) -> str:
    if not _COMMAND_NAME.fullmatch(name):
        raise ConfigurationError(
            f"command name {name!r} would be rejected by Telegram: names are "
            f"1-32 characters of lowercase a-z, 0-9 and underscore. PTB would "
            f"still dispatch it, but publishing it would silently freeze every "
            f"command menu at whatever the previous run wrote."
        )

    return name


def split_docstring(fn: Callable[..., Any]) -> tuple[str, str]:
    """Split a handler docstring into (summary, detail body).

    The summary is the whole first *paragraph* -- not the first line, because a
    summary that wraps across two source lines would otherwise be cut at the
    wrap, and the orphaned remainder would open the detail text. Everything
    after the first blank line is the detail body, shown by ``/help <command>``.
    """
    # cleandoc already drops leading blank lines, so the paragraph starts at 0.
    lines = inspect.cleandoc(fn.__doc__ or "").splitlines()
    end = next((i for i, line in enumerate(lines) if not line.strip()), len(lines))

    summary = " ".join(line.strip() for line in lines[:end])
    detail = "\n".join(lines[end:]).strip()

    return summary, detail


@dataclass
class Registration:
    """One declaratively-registered handler and its metadata."""

    #: the transport: which PTB handler carries the update. Says nothing about
    #: how extra parameters are filled -- that is ``args`` alone.
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
    edits: bool = False  # opt in to receiving edited messages
    #: whether extra handler params are parsed command arguments (meaningful
    #: only when the transport is a command). True for router commands; a
    #: conversation entry only opts in via ``args=True`` (its extra params are
    #: injected otherwise), and /help must not present injected parameters as
    #: a usage line.
    args: bool = True
    middlewares: list[Middleware] = field(default_factory=list)


def _minus_edits(base: Any, edits: bool) -> Any:
    return base if edits else base & ~ptb_filters.UpdateType.EDITED


def ptb_handler(reg: Registration, callback: Callable[..., Any]) -> Any:
    """The one PTB handler a registration implies.

    Every registration -- the bot's flat handlers and every conversation step
    -- is wired through here, so the edit-exclusion rule cannot drift between
    call sites. The rule: take what the handler would otherwise match and
    subtract ``UpdateType.EDITED``, unless ``edits=True`` -- exclude edits,
    and nothing else. Subtracting matters: message handlers matched channel
    posts and business messages before the exclusion existed and must keep
    matching them, while commands matched only PTB's ``CommandHandler``
    default (``UpdateType.MESSAGES``) and must not quietly widen beyond it.
    The default is spelled out here so there is a baseline to subtract from,
    instead of a replacement filter that encodes the reach and the exclusion
    as one opaque choice.
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
        # when= narrows inside the pattern: a rejected press stays available
        # to a sibling handler on the same callback model. Presses have no
        # edited form, so the edit rule does not apply.
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
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a ``/command`` handler; extra params become typed arguments.

        ``edits=True`` opts in to also receiving edited messages -- by default
        an edited ``/command`` is ignored, because Telegram redelivers the whole
        message and editing an old command would otherwise re-run it.
        """

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
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
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a handler for one typed callback-data model.

        The decoded, validated instance is injected as the ``data`` parameter.
        ``when`` optionally narrows the match on the decoded payload: a press
        this handler's ``when`` rejects stays available to another handler on
        the same model rather than being swallowed.

        Callback handlers belong in the default group. A catch-all sits last in
        that group to answer presses no handler took (Telegram spins the button
        until *something* answers), Telegram accepts one answer per press, and
        PTB runs groups independently -- so a callback handler in a later group
        still runs, but its answer (and any alert) arrives too late to display.
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
        self, filters: Any = None, *, edits: bool = False, group: int = 0
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a message handler with PTB filters (defaults to text messages).

        Edited messages are excluded unless ``edits=True``: an edit arrives as
        a fresh update carrying the whole message, so without the exclusion,
        editing any old text would re-feed it to this handler.
        """

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.registrations.append(
                Registration(
                    kind="message",
                    fn=fn,
                    name=fn.__name__,
                    filters=filters,
                    edits=edits,
                    group=group,
                )
            )

            return fn

        return register

    def reply_button(
        self, *buttons: ReplyButton | str, group: int = 0
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a handler for presses of one or more reply-keyboard buttons.

        A press arrives as a plain text message equal to the button label, so
        this is sugar for a message handler with an exact-text filter. The full
        pipeline applies (DI, guards, middleware), and a returned
        :class:`~vitrine.screens.Screen` replies — a persistent keyboard set at
        ``/start`` becomes a launcher that works from anywhere.
        """
        labels = [b.text if isinstance(b, ReplyButton) else b for b in buttons]
        if not labels:
            raise ValueError("reply_button needs at least one button or label")

        return self.message(ptb_filters.Text(labels), group=group)

    def middleware(self, mw: Middleware) -> Middleware:
        """Attach middleware to every handler on this router and its children."""
        self.middlewares.append(mw)
        return mw

    def include(self, router: Router) -> None:
        """Mount a sub-router."""
        self.children.append(router)

    def conversation(self, conversation: Conversation) -> Conversation:
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
