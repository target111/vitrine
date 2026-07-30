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

from telegram.ext import filters as ptb_filters

from .callbacks import CallbackData
from .exceptions import ConfigurationError
from .middleware import Middleware
from .screens import ReplyButton

if TYPE_CHECKING:
    from .conversations import Conversation

#: what a message handler matches when the caller names no filters
DEFAULT_MESSAGE_FILTERS = ptb_filters.TEXT & ~ptb_filters.COMMAND

#: Telegram's rule for ``BotCommand.command``
_COMMAND_NAME = re.compile(r"^[a-z0-9_]{1,32}$")


def validate_command_name(command: str, owner: str) -> str:
    """Reject at registration a command ``setMyCommands`` would refuse.

    PTB's ``CommandHandler`` lowercases a command before it validates it, so
    ``/myCmd`` dispatches happily while that same name in the published menu
    makes Telegram reject the *whole* batch -- leaving every menu, in every
    scope, frozen at what the previous run published, with only a log line to
    say so. Cheaper as an import-time error.
    """
    if not _COMMAND_NAME.match(command):
        raise ConfigurationError(
            f"{owner}: {command!r} is not a valid command name. Telegram allows "
            f"1-32 characters from a-z, 0-9 and underscore -- no uppercase."
        )

    return command


#: Telegram delivers an edit as a new update carrying the whole message, and
#: PTB's defaults match those too: editing an old ``/help`` into ``/whatever``
#: runs ``/whatever``, and editing any old text message re-feeds it to message
#: handlers -- including whichever conversation state is live *now*, several
#: steps past the one that first read it. Handlers are narrowed to this unless
#: they ask for edits with ``edits=True``.
FRESH_MESSAGES = ptb_filters.UpdateType.MESSAGE


def command_filters(*, edits: bool) -> Any:
    """Filters for a ``CommandHandler``; ``None`` leaves PTB's own default."""
    return None if edits else FRESH_MESSAGES


def message_filters(filters: Any, *, edits: bool) -> Any:
    """Filters for a ``MessageHandler``; ``None`` means the text default."""
    base = DEFAULT_MESSAGE_FILTERS if filters is None else filters

    return base if edits else base & FRESH_MESSAGES


def _doc_parts(fn: Callable[..., Any]) -> tuple[str, str]:
    """Split a docstring into its summary paragraph and everything after it.

    The split is the first blank line, not the first newline: a summary that
    wraps across two source lines is still one sentence, and cutting it at the
    wrap truncated the command description mid-phrase -- in ``/help``, and in
    the description published to Telegram's command menu.
    """
    summary, _, body = inspect.cleandoc(fn.__doc__ or "").partition("\n\n")

    return " ".join(summary.split()), body.strip()


def doc_summary(fn: Callable[..., Any]) -> str:
    """A handler's summary: the docstring's first paragraph, as one line.

    The default command description. ``""`` when there is nothing to take,
    including a docstring that is only whitespace.
    """
    return _doc_parts(fn)[0]


def doc_body(fn: Callable[..., Any]) -> str:
    """Everything in the docstring after the summary paragraph, dedented.

    The summary is already the command's one-line description, so this is the
    detail ``/help <command>`` has room for and the command list does not.
    """
    return _doc_parts(fn)[1]


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
    edits: bool = False  # also match edited messages
    #: False for a registration that exists only to be discovered, so
    #: ``/help <command>`` does not read arguments off a signature that the
    #: dispatcher never parses -- see ``Conversation.command_registrations``
    parses_args: bool = True
    group: int = 0
    middlewares: list[Middleware] = field(default_factory=list)


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

        ``edits=True`` also fires the handler when a user edits an older
        message into this command -- off by default, see :data:`FRESH_MESSAGES`.
        """

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            desc = doc_summary(fn) if description is None else description

            self.registrations.append(
                Registration(
                    kind="command",
                    fn=fn,
                    name=fn.__name__,
                    command=validate_command_name(
                        command or fn.__name__, f"handler {fn.__name__!r}"
                    ),
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
        self, filters: Any = None, *, edits: bool = False, group: int = 0
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a message handler with PTB filters (defaults to text messages).

        ``edits=True`` also fires the handler for edits of older messages --
        off by default, see :data:`FRESH_MESSAGES`.
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
