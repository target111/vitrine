"""The Bot: assembles routers, providers, auth, workers, and PTB.

Small mode::

    bot = Bot(token=os.environ["BOT_TOKEN"])

    @bot.command("start")
    async def start(update):
        return Screen(text="hi!", keyboard=[[Button("Ping", callback="ping")]])

    bot.run()

Scaled mode: build routers/conversations in separate packages, register
providers for domain services, hand an :class:`~vitrine.auth.Auth` for the
app's principal type, and mount everything here. The underlying PTB
``Application`` stays fully reachable via ``bot.build()`` / ``bot.application``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Generic, TypeVar

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackContext,
    CallbackQueryHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

from . import commands as command_discovery
from .auth import Auth
from .commands import CommandMenus, ScopeChats, resolve_chats
from .conversations import Conversation
from .dispatch import Dispatch
from .errors import ErrorRegistry
from .exceptions import ConfigurationError
from .injection import Providers
from .media import FileIdCache, InMemoryFileIdCache
from .middleware import Middleware
from .ratelimit import RateLimiter
from .routing import Registration, Router, ptb_handler
from .screens import DELIVERY_KEY, Delivery
from .workers import WorkerSpec, WorkerSupervisor

logger = logging.getLogger("vitrine.app")

P = TypeVar("P")


class VitrineContext(CallbackContext[ExtBot, dict, dict, dict]):
    """Default context: a plain CallbackContext that allows framework attributes."""


class Bot(Generic[P]):
    def __init__(
        self,
        token: str = "",
        *,
        auth: Auth[P] | None = None,
        markdown_version: int = 2,
        help_command: bool = True,
        file_ids: FileIdCache | None = None,
        scope_chats: dict[str, ScopeChats] | None = None,
        scope_member: Callable[[str, P | None], bool] | None = None,
        known_scope_chats: ScopeChats | None = None,
        strict_types: bool = False,
        context_type: type[CallbackContext] | None = None,
    ) -> None:
        self.token = token
        self.auth = auth
        self.markdown_version = markdown_version
        self._help_command = help_command
        self._scope_chats = scope_chats or {}
        if "default" in self._scope_chats:
            # Every scoped chat's menu already starts from the default
            # commands; naming "default" here would list each of them twice.
            raise ConfigurationError(
                'scope_chats must not name "default": the default commands are '
                "already part of every scoped chat's menu"
            )
        self._scope_member = scope_member
        self._strict_types = strict_types
        #: chats that may carry a menu from an earlier run -- a recovery seed,
        #: not steady state. See CommandMenus for when it is consumed.
        self._menus = CommandMenus(known_scope_chats)
        self._context_type = context_type or VitrineContext

        self.router = Router("root")
        self.providers = Providers()
        self.errors = ErrorRegistry()
        self.limiter = RateLimiter()
        self.file_ids: FileIdCache = file_ids or InMemoryFileIdCache()
        self._middlewares: list[Middleware] = []
        self._startup_hooks: list[Callable[..., Awaitable[Any]]] = []
        self._shutdown_hooks: list[Callable[..., Awaitable[Any]]] = []
        self._worker_specs: list[WorkerSpec] = []

        self.application: Application | None = None
        self.delivery: Delivery | None = None
        self._dispatch: Dispatch | None = None
        self._supervisor: WorkerSupervisor | None = None
        self._registrations: list[Registration] = []

    # -- registration (delegates to the root router) ---------------------------

    def command(self, *args: Any, **kwargs: Any) -> Any:
        return self.router.command(*args, **kwargs)

    def callback(self, *args: Any, **kwargs: Any) -> Any:
        return self.router.callback(*args, **kwargs)

    def message(self, *args: Any, **kwargs: Any) -> Any:
        return self.router.message(*args, **kwargs)

    def reply_button(self, *args: Any, **kwargs: Any) -> Any:
        return self.router.reply_button(*args, **kwargs)

    def conversation(self, conversation: Conversation) -> Conversation:
        return self.router.conversation(conversation)

    def include(self, router: Router) -> None:
        self.router.include(router)

    def raw(self, handler: Any, group: int = 0) -> Any:
        return self.router.raw(handler, group)

    def middleware(self, mw: Middleware) -> Middleware:
        """Bot-scoped middleware: wraps every framework-handled update."""
        self._middlewares.append(mw)
        return mw

    # -- providers ---------------------------------------------------------------

    def provide(self, name: str | Callable[..., Any] | None = None) -> Any:
        """Register a provider: ``@bot.provide("db")`` or ``@bot.provide`` (uses the fn name)."""  # noqa: E501
        if callable(name):
            self.providers.register(name.__name__, name)
            return name

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.providers.register(name or fn.__name__, fn)
            return fn

        return register

    def provide_value(self, name: str, value: Any) -> None:
        """Register a constant (a config object, a pool created at startup, ...)."""
        self.providers.register_value(name, value)

    # -- lifecycle ---------------------------------------------------------------

    def on_startup(
        self, fn: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        self._startup_hooks.append(fn)
        return fn

    def on_shutdown(
        self, fn: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        self._shutdown_hooks.append(fn)
        return fn

    def worker(
        self,
        every: float | None = None,
        *,
        name: str | None = None,
        initial_delay: float = 0.0,
        backoff_max: float = 60.0,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """Register a supervised background worker (periodic if ``every`` is set)."""

        def register(
            fn: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            self._worker_specs.append(
                WorkerSpec(
                    fn=fn,
                    name=name or fn.__name__,
                    every=every,
                    initial_delay=initial_delay,
                    backoff_max=backoff_max,
                )
            )
            return fn

        return register

    def on_error(self, exc_type: type[BaseException]) -> Any:
        """Register friendly UX for an exception type (most specific wins)."""
        return self.errors.on(exc_type)

    # -- building ---------------------------------------------------------------

    def build(self) -> Application:
        """Create and wire the PTB Application (idempotent)."""
        if self.application is not None:
            return self.application

        if not self.token:
            raise ConfigurationError("Bot needs a token to build the PTB application")

        application = (
            Application.builder()
            .token(self.token)
            .context_types(ContextTypes(context=self._context_type))
            .post_init(self._post_init)
            .post_stop(self._post_stop)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        self.application = application
        self._attach_delivery(
            Delivery(application.bot, self.file_ids, markdown_version=self.markdown_version)
        )
        application.bot_data[DELIVERY_KEY] = self.delivery

        if self.auth is not None:
            application.add_handler(TypeHandler(Update, self._ban_gate), group=-100)

        for handler, group in self._wire_handlers():
            application.add_handler(handler, group=group)
        application.add_error_handler(self._on_ptb_error)

        return application

    def _make_dispatch(self) -> Dispatch:
        if self._dispatch is None:
            self._dispatch = Dispatch(
                self.providers,
                self.errors,
                self.limiter,
                self.auth,
                self._middlewares,
                strict_types=self._strict_types,
            )

        return self._dispatch

    def _attach_delivery(self, delivery: Delivery) -> None:
        self.delivery = delivery
        self._make_dispatch().delivery = delivery
        self.provide_value("delivery", delivery)

    def _wire_handlers(self) -> list[tuple[Any, int]]:
        """Turn all registrations into PTB handlers. Shared by build() and tests."""
        dispatch = self._make_dispatch()
        handler_regs = list(self.router.walk())
        conversations = [conv for conv, _ in self.router.walk_conversations()]
        # A conversation's entry commands are handled by its own
        # ConversationHandler, but they are still commands: they belong in
        # /help and in the Telegram command menu like any other.
        conv_regs = [reg for conv in conversations for reg in conv.command_registrations()]

        if self._help_command and not any(
            reg.command == "help"
            for reg in (*handler_regs, *conv_regs)
            if reg.kind == "command"
        ):
            handler_regs.append(self._help_registration())

        self._registrations = [*handler_regs, *conv_regs]
        self._check_scopes(self._registrations)

        wired: list[tuple[Any, int]] = []
        for reg in handler_regs:
            dispatch.validate(reg)
            wired.append((ptb_handler(reg, dispatch.ptb_callback(reg)), reg.group))

        for conv, middlewares in self.router.walk_conversations():
            wired.append((conv.build(dispatch, middlewares), 0))

        # Peers are linked after every handler exists, since ending a run
        # means reaching into the ConversationHandler that owns it.
        exclusive = [conv for conv in conversations if conv.exclusive]
        for conv in exclusive:
            conv.link_peers(exclusive)
        for handler, group in self.router.walk_raw():
            wired.append((handler, group))
        # Catch-all: a press no handler took -- a NOOP button, or one every
        # when= on its model rejected -- still gets answered, so Telegram
        # doesn't spin the button until timeout. It sits *last in the default
        # group*: Telegram accepts one answer per press and PTB runs groups
        # independently, so a callback handler in a later group still runs but
        # its alert arrives too late to display. Callback handlers belong in
        # the default group.
        wired.append((CallbackQueryHandler(_answer_stray), 0))

        return wired

    def _check_scopes(self, registrations: Sequence[Registration]) -> None:
        """A command scope no chat resolver knows is almost always a typo.

        ``scope="admins"`` against ``scope_chats={"admin": ...}`` reaches
        ``/help`` and nobody's Telegram menu. Only checked when ``scope_chats``
        is configured at all -- without it, ``scope`` is just a /help grouping.
        """
        if not self._scope_chats:
            return

        # The same filter the menus themselves apply, so a registration this
        # check clears is exactly one a menu would carry.
        for reg in command_discovery.menu_commands(registrations):
            if reg.scope == "default":
                continue
            if reg.scope not in self._scope_chats:
                raise ConfigurationError(
                    f"command /{reg.command} has scope {reg.scope!r}, but "
                    f"scope_chats only knows {sorted(self._scope_chats)}: no "
                    f"chat would ever receive it in a menu"
                )

    # -- auto /help ---------------------------------------------------------------

    def _help_registration(self) -> Registration:
        async def help_command(update: Any, context: Any, command: str = "") -> Any:
            """Show available commands.

            Name one command to see it in full: usage, arguments, and what
            governs it (scope, guards, rate limit).
            """
            scopes = await self._visible_scopes(update, context)
            if not command.strip():
                return command_discovery.help_screen(self._registrations, scopes)

            reg = command_discovery.resolve_command(self._registrations, scopes, command)
            if reg is None:
                # Invisible answers exactly like nonexistent: /help ban must
                # not confirm to a non-admin that /ban is a thing.
                return command_discovery.unknown_command_screen(command)

            specs = self._make_dispatch().arg_specs(reg)
            return command_discovery.command_detail_screen(reg, specs)

        return Registration(
            kind="command",
            fn=help_command,
            name="help",
            command="help",
            description="Show available commands",
        )

    async def _visible_scopes(self, update: Any, context: Any) -> set[str]:
        scopes = {"default"}
        all_scopes = {
            reg.scope for reg in self._registrations if reg.kind == "command"
        } | set(self._scope_chats)

        principal: P | None = None
        if self.auth is not None:
            inv = self._make_dispatch().invocation("help", update, context)
            try:
                principal = await self.auth.resolve(inv, self.providers)
            finally:
                await inv.aclose()

        for scope in all_scopes - {"default"}:
            if self._scope_member is not None:
                if self._scope_member(scope, principal):
                    scopes.add(scope)
            elif self.auth is not None and self.auth.admin(principal):
                scopes.add(scope)

        return scopes

    # -- runtime ---------------------------------------------------------------

    async def _ban_gate(self, update: Any, context: Any) -> None:
        """Bot-wide ban enforcement, before any handler in any group."""
        assert self.auth is not None
        dispatch = self._make_dispatch()
        inv = dispatch.invocation("ban-gate", update, context)
        try:
            principal = await self.auth.resolve(inv, self.providers)
        except Exception:
            logger.exception("principal resolution failed in ban gate")
            return
        finally:
            await inv.aclose()

        if self.auth.banned(principal):
            query = getattr(update, "callback_query", None)
            if query is not None:
                try:
                    await query.answer(
                        "You are banned from using this bot.", show_alert=True
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise ApplicationHandlerStop

    async def _post_init(self, application: Application) -> None:
        dispatch = self._make_dispatch()
        for hook in self._startup_hooks:
            await self._run_hook(hook)

        self._supervisor = WorkerSupervisor(
            self.providers, lambda name: dispatch.invocation(name)
        )
        for spec in self._worker_specs:
            self._supervisor.add(spec)
        self._supervisor.start()

        try:
            # The same path a later bot.sync_commands() takes, not a parallel
            # one -- and a failure (database still coming up, say) must not
            # break startup: the CommandMenus seed survives for the next sync.
            await self.sync_commands()
        except Exception:
            logger.exception("could not sync command menus")

    async def _post_stop(self, application: Application) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()

    async def _post_shutdown(self, application: Application) -> None:
        for hook in self._shutdown_hooks:
            await self._run_hook(hook)

    async def _run_hook(self, hook: Callable[..., Awaitable[Any]]) -> None:
        from .injection import resolve_kwargs

        inv = self._make_dispatch().invocation(f"hook:{hook.__name__}")
        try:
            kwargs = await resolve_kwargs(hook, inv, self.providers)
            await hook(**kwargs)
        finally:
            await inv.aclose()

    async def sync_commands(self, chats: Sequence[int] | None = None) -> None:
        """Republish Telegram command menus so membership changes take effect.

        Call after promoting or demoting someone instead of restarting; pass
        ``chats=[...]`` after a single membership change to touch only those
        chats (nothing else written, nothing else cleared, not even the
        default menu). Startup runs the very same path.
        """
        if self.application is None:
            raise ConfigurationError(
                "sync_commands() needs the PTB application; call build() first"
            )

        await self._menus.sync(
            self.application.bot,
            self._registrations,
            self._resolve_scope_chats,
            chats=chats,
        )

    async def _resolve_scope_chats(self) -> dict[str, Sequence[int]]:
        # Handed to CommandMenus.sync unevaluated: it reads the scopes under
        # the same lock it writes them, so an older read cannot land last.
        # Resolvers are independent and typically each hit a database, so the
        # startup cost is the slowest one rather than the sum.
        resolved = await asyncio.gather(
            *(resolve_chats(chats) for chats in self._scope_chats.values())
        )

        return dict(zip(self._scope_chats, resolved, strict=True))

    async def _on_ptb_error(self, update: Any, context: Any) -> None:
        """Dispatcher-level catch-all for errors outside the pipeline."""
        error = context.error
        if error is None:
            return

        inv = self._make_dispatch().invocation("ptb-error", update, context)
        try:
            await self.errors.dispatch(error, inv, self.providers)
        finally:
            await inv.aclose()

    def run(self, *, allowed_updates: Any = Update.ALL_TYPES) -> None:
        """Build and run with long polling. For webhooks, use ``build()`` and PTB directly."""  # noqa
        application = self.build()
        application.run_polling(allowed_updates=allowed_updates)


async def _answer_stray(update: Any, context: Any) -> None:
    """Answer a press nothing claimed, so the button doesn't spin to timeout."""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:  # noqa: BLE001
            pass
