"""The Bot: assembles routers, providers, auth, workers, and PTB.

Small mode::

    bot = Bot(token=os.environ["BOT_TOKEN"])

    @bot.command("start")
    async def start(update):
        return Screen(text="hi!", keyboard=[[Button("Ping", callback="ping")]])

    bot.run()

Scaled mode: build routers/conversations in separate packages, register
providers for domain services, hand an :class:`~vitrine.auth.Auth` for the
app's principal type, and mount everything here. ``bot.build()`` returns the
wired PTB ``Application`` (for webhooks, or ``vitrine.testing``); after it,
registration is closed.
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
    BaseHandler,
    CallbackContext,
    CallbackQueryHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)
from telegram.request import BaseRequest

from . import commands as command_discovery
from .args import Arg
from .auth import Auth
from .callbacks import CallbackData
from .commands import CommandMenus, MenuStore, ScopeChats, resolve_chats
from .conversations import Conversation, ConversationDispatcher
from .dispatch import Dispatch
from .errors import ErrorRegistry
from .exceptions import ConfigurationError
from .injection import Providers, resolve_kwargs, run_in_update_scope
from .media import FileIdCache, InMemoryFileIdCache
from .middleware import Middleware
from .ratelimit import RateLimiter
from .routing import Registration, Router, ptb_handler
from .screens import DELIVERY_KEY, Delivery, ReplyButton
from .workers import WorkerSpec, WorkerSupervisor

logger = logging.getLogger("vitrine.app")

P = TypeVar("P")
F = TypeVar("F", bound=Callable[..., Any])
Hook = TypeVar("Hook", bound=Callable[..., Awaitable[Any]])


class VitrineContext(CallbackContext[ExtBot, dict, dict, dict]):
    """Default context: a plain CallbackContext that allows framework attributes."""


class VitrineApplication(Application):
    """PTB's Application, with one dependency scope per processed update.

    Every handler that runs for an update -- the ban gate, each handler group,
    error handlers -- shares that scope, and its provider cleanups run after
    the last of them. A cleanup that fails goes to the error handlers.
    """

    async def process_update(self, update: object) -> None:
        process = super().process_update
        await run_in_update_scope(
            update,
            lambda: process(update),
            on_cleanup_error=lambda exc: self.process_error(update, exc),
        )


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
        menu_store: MenuStore | None = None,
        strict_types: bool = False,
        context_type: type[CallbackContext] | None = None,
    ) -> None:
        """
        ``scope_chats`` maps each command scope to the chats it covers; it
        drives both the Telegram menus and who sees the scope in ``/help``.
        Without it, scopes only group ``/help``, and ``scope_member`` (or,
        failing that, ``Auth.is_admin``) decides who sees them -- so the two
        are mutually exclusive.
        """
        self.token = token
        self.auth = auth
        self.markdown_version = markdown_version
        self._help_command = help_command
        self._scope_chats = scope_chats or {}
        if "default" in self._scope_chats:
            raise ConfigurationError(
                'scope_chats must not name "default": the default commands are '
                "already part of every scoped chat's menu"
            )
        if self._scope_chats and scope_member is not None:
            raise ConfigurationError(
                "pass scope_chats or scope_member, not both: with scope_chats, a "
                "chat's scopes in /help are the ones its command menu shows"
            )
        self._scope_member = scope_member
        self._strict_types = strict_types
        self._menus = CommandMenus(known_scope_chats, menu_store)
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

        # set by build()
        self.application: Application | None = None
        self.delivery: Delivery | None = None
        self._dispatch: Dispatch | None = None
        self._transports: tuple[BaseRequest | None, BaseRequest | None] = (None, None)
        self._registrations: list[Registration] = []
        self._conversations: list[ConversationDispatcher] = []

        # set by startup()
        self._supervisor: WorkerSupervisor | None = None
        self._menu_task: asyncio.Task[None] | None = None

    def _check_mutable(self) -> None:
        if self.application is not None:
            raise ConfigurationError(
                "the bot is already built; register everything before build() or run()"
            )

    # -- registration (delegates to the root router) ---------------------------

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
        """Register a ``/command`` handler; ``Arg[...]`` params are its arguments."""
        return self.router.command(
            command,
            description=description,
            scope=scope,
            hidden=hidden,
            edits=edits,
            group=group,
        )

    def callback(
        self,
        model: type[CallbackData],
        *,
        when: Callable[[Any], bool] | None = None,
        group: int = 0,
    ) -> Callable[[F], F]:
        """Register a handler for one typed callback-data model (injected as ``data``)."""
        return self.router.callback(model, when=when, group=group)

    def message(
        self, filters: Any = None, *, edits: bool = False, group: int = 0
    ) -> Callable[[F], F]:
        """Register a message handler with PTB filters (defaults to text messages)."""
        return self.router.message(filters, edits=edits, group=group)

    def reply_button(self, *buttons: ReplyButton | str, group: int = 0) -> Callable[[F], F]:
        """Register a handler for presses of one or more reply-keyboard buttons."""
        return self.router.reply_button(*buttons, group=group)

    def conversation(self, conversation: Conversation) -> Conversation:
        return self.router.conversation(conversation)

    def include(self, router: Router) -> None:
        self.router.include(router)

    def raw(self, handler: BaseHandler, group: int = 0) -> BaseHandler:
        return self.router.raw(handler, group)

    def middleware(self, mw: Middleware) -> Middleware:
        """Bot-scoped middleware: wraps every framework-handled update."""
        self._check_mutable()
        self._middlewares.append(mw)
        return mw

    # -- providers ---------------------------------------------------------------

    def provide(self, name: str | Callable[..., Any] | None = None) -> Any:
        """Register a provider: ``@bot.provide("db")`` or ``@bot.provide`` (fn name)."""
        self._check_mutable()
        if callable(name):
            self.providers.register(name.__name__, name)
            return name

        def register(fn: F) -> F:
            self.providers.register(name or fn.__name__, fn)
            return fn

        return register

    def provide_value(self, name: str, value: Any) -> None:
        """Register a constant (a config object, a pool created at startup, ...)."""
        self._check_mutable()
        self.providers.register_value(name, value)

    # -- lifecycle ---------------------------------------------------------------

    def on_startup(self, fn: Hook) -> Hook:
        self._check_mutable()
        self._startup_hooks.append(fn)
        return fn

    def on_shutdown(self, fn: Hook) -> Hook:
        self._check_mutable()
        self._shutdown_hooks.append(fn)
        return fn

    def worker(
        self,
        every: float | None = None,
        *,
        name: str | None = None,
        initial_delay: float = 0.0,
        backoff_max: float = 60.0,
    ) -> Callable[[Hook], Hook]:
        """Register a supervised background worker (periodic if ``every`` is set)."""
        self._check_mutable()

        def register(fn: Hook) -> Hook:
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

    def on_error(self, exc_type: type[BaseException]) -> Callable[[Hook], Hook]:
        """Register friendly UX for an exception type (most specific wins)."""
        self._check_mutable()
        return self.errors.on(exc_type)

    # -- building ---------------------------------------------------------------

    def build(
        self,
        *,
        request: BaseRequest | None = None,
        get_updates_request: BaseRequest | None = None,
    ) -> Application:
        """Create and wire the PTB Application (idempotent); closes registration.

        ``request`` replaces PTB's HTTP transport for Bot API calls -- a
        proxy-aware client, or :class:`vitrine.testing.FakeTelegram` in tests --
        and ``get_updates_request`` the one polling uses. Give them separate
        instances, as PTB does: a long poll holds its connection, so sharing
        one starves every other call. Once built, the transports are fixed;
        passing different ones raises instead of being ignored.
        """
        transports = (request, get_updates_request)
        if self.application is not None:
            if any(
                new is not None and new is not old
                for new, old in zip(transports, self._transports, strict=True)
            ):
                raise ConfigurationError(
                    "the bot is already built with other HTTP transports; build a "
                    "fresh Bot for each (e.g. one per TestClient, from a factory)"
                )
            return self.application

        if not self.token:
            raise ConfigurationError("Bot needs a token to build the PTB application")

        builder = (
            Application.builder()
            .application_class(VitrineApplication)
            .token(self.token)
            .context_types(ContextTypes(context=self._context_type))
            .post_init(self._post_init)
            .post_stop(self._post_stop)
            .post_shutdown(self._post_shutdown)
        )
        if request is not None:
            builder = builder.request(request)
        if get_updates_request is not None:
            builder = builder.get_updates_request(get_updates_request)
        application = builder.build()

        delivery = Delivery(
            application.bot, self.file_ids, markdown_version=self.markdown_version
        )
        dispatch = Dispatch(
            self.providers,
            self.errors,
            self.limiter,
            delivery,
            self.auth,
            self._middlewares,
            strict_types=self._strict_types,
        )
        handlers = self._compile(dispatch)

        application.bot_data[DELIVERY_KEY] = delivery
        if self.auth is not None:
            application.add_handler(TypeHandler(Update, self._ban_gate), group=-100)
        for handler, group in handlers:
            application.add_handler(handler, group=group)
        application.add_error_handler(self._on_ptb_error)

        self.delivery = delivery
        self._dispatch = dispatch
        self._transports = transports
        self.application = application
        self.router.freeze()

        return application

    def _compile(self, dispatch: Dispatch) -> list[tuple[BaseHandler, int]]:
        """Validate every registration and turn it into PTB handlers, in order."""
        flat = list(self.router.walk())
        mounted = list(self.router.walk_conversations())
        conv_entries = [reg for conv, _ in mounted for reg in conv.entry_registrations()]
        conv_regs = [reg for reg in conv_entries if reg.kind == "command"]

        if self._help_command and not any(
            reg.kind == "command" and reg.command == "help" for reg in (*flat, *conv_regs)
        ):
            flat.append(self._help_registration())

        self._registrations = [*flat, *conv_regs]
        self._check_scopes(self._registrations)
        _check_reachable(flat, conv_entries)

        for reg in flat:
            dispatch.validate(reg)
        self._conversations = [conv.build(dispatch, mws) for conv, mws in mounted]
        exclusive = [d for d in self._conversations if d.conv.exclusive]
        for d in exclusive:
            d.peers = [peer for peer in exclusive if peer is not d]

        def wire(regs: list[Registration]) -> list[tuple[BaseHandler, int]]:
            return [
                (ptb_handler(reg, dispatch.ptb_callback(reg)), reg.group) for reg in regs
            ]

        # PTB runs the first matching handler of each group. Within a group:
        #   1. reply-keyboard presses -- navigation works from anywhere;
        #   2. conversations -- entry points and a live run's steps see their
        #      updates before any general-purpose handler does;
        #   3. every other handler, in registration order, then raw handlers;
        #   4. the catch-all that answers presses nothing claimed. Telegram takes
        #      one answer per press, so callback handlers belong in group 0.
        return [
            *wire([reg for reg in flat if reg.reply_button]),
            *((d, 0) for d in self._conversations),
            *wire([reg for reg in flat if not reg.reply_button]),
            *self.router.walk_raw(),
            (CallbackQueryHandler(_answer_stray), 0),
        ]

    def _check_scopes(self, registrations: Sequence[Registration]) -> None:
        """A command scope ``scope_chats`` doesn't know is almost always a typo."""
        if not self._scope_chats:
            return

        for reg in command_discovery.menu_commands(registrations):
            if reg.scope != "default" and reg.scope not in self._scope_chats:
                raise ConfigurationError(
                    f"command /{reg.command} has scope {reg.scope!r}, but "
                    f"scope_chats only knows {sorted(self._scope_chats)}: no "
                    f"chat would ever receive it in a menu"
                )

    # -- auto /help ---------------------------------------------------------------

    def _help_registration(self) -> Registration:
        async def help_command(update: Any, context: Any, command: Arg[str] = "") -> Any:
            """Show available commands.

            Name one command to see it in full: usage, arguments, and what
            governs it (scope, guards, rate limit).
            """
            scopes = await self._visible_scopes(update, context)
            if not command.strip():
                return command_discovery.help_screen(self._registrations, scopes)

            reg = command_discovery.resolve_command(self._registrations, scopes, command)
            if reg is None:
                # invisible answers exactly like nonexistent
                return command_discovery.unknown_command_screen(command)

            specs = self._built().arg_specs(reg)
            return command_discovery.command_detail_screen(reg, specs)

        return Registration(
            kind="command",
            fn=help_command,
            name="help",
            command="help",
            description="Show available commands",
        )

    async def _visible_scopes(self, update: Any, context: Any) -> set[str]:
        """The scopes whose commands this caller sees in /help.

        With ``scope_chats``: the scopes naming this chat -- exactly what the
        chat's command menu holds. Otherwise ``scope_member``, or admins see
        every scope.
        """
        scopes = {"default"}
        if self._scope_chats:
            chat = getattr(update, "effective_chat", None)
            if chat is not None:
                resolved = await self._resolve_scope_chats()
                scopes |= {name for name, ids in resolved.items() if chat.id in ids}
            return scopes

        named = {reg.scope for reg in self._registrations if reg.kind == "command"}
        principal: P | None = None
        if self.auth is not None:
            inv = self._built().invocation("help", update, context)
            try:
                principal = await self.auth.resolve(inv, self.providers)
            finally:
                await inv.aclose()

        for scope in named - {"default"}:
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
        inv = self._built().invocation("ban-gate", update, context)
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

    async def startup(self) -> None:
        """Run startup hooks, start workers, and sync command menus in the background.

        ``run()`` (and PTB's ``run_polling``/``run_webhook`` on the built
        application) call this; call it yourself only when driving the
        application some other way.
        """
        dispatch = self._built()
        for hook in self._startup_hooks:
            await self._run_hook(hook)

        self._supervisor = WorkerSupervisor(
            self.providers, lambda name: dispatch.invocation(name)
        )
        for spec in self._worker_specs:
            self._supervisor.add(spec)
        self._supervisor.start()

        # One API call per scoped chat: don't make the bot wait for it.
        self._menu_task = asyncio.create_task(
            self._initial_menu_sync(), name="vitrine-menu-sync"
        )

    async def _initial_menu_sync(self) -> None:
        try:
            await self.sync_commands()
        except Exception:
            # the known_scope_chats seed survives for the next sync
            logger.exception("could not sync command menus")

    async def stop(self) -> None:
        """Stop the menu sync, workers and conversation timeouts."""
        if self._menu_task is not None:
            self._menu_task.cancel()
            await asyncio.gather(self._menu_task, return_exceptions=True)
            self._menu_task = None
        if self._supervisor is not None:
            await self._supervisor.stop()
            self._supervisor = None
        for conversation in self._conversations:
            conversation.close()

    async def shutdown(self) -> None:
        """Run shutdown hooks."""
        for hook in self._shutdown_hooks:
            await self._run_hook(hook)

    async def _post_init(self, application: Application) -> None:
        await self.startup()

    async def _post_stop(self, application: Application) -> None:
        await self.stop()

    async def _post_shutdown(self, application: Application) -> None:
        await self.shutdown()

    def _built(self) -> Dispatch:
        if self._dispatch is None:
            raise ConfigurationError("the bot isn't built yet; call build() first")
        return self._dispatch

    async def _run_hook(self, hook: Callable[..., Awaitable[Any]]) -> None:
        inv = self._built().invocation(f"hook:{hook.__name__}")
        try:
            kwargs = await resolve_kwargs(hook, inv, self.providers)
            await hook(**kwargs)
        finally:
            await inv.aclose()

    async def sync_commands(self, chats: Sequence[int] | None = None) -> None:
        """Republish Telegram command menus so membership changes take effect.

        Call after promoting or demoting someone instead of restarting; pass
        ``chats=[...]`` after a single membership change to touch only those
        chats (nothing else written or cleared, not even the default menu).
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
        # resolvers are independent (typically one query each): run them together
        resolved = await asyncio.gather(
            *(resolve_chats(chats) for chats in self._scope_chats.values())
        )

        return dict(zip(self._scope_chats, resolved, strict=True))

    async def _on_ptb_error(self, update: Any, context: Any) -> None:
        """Dispatcher-level catch-all for errors outside the pipeline."""
        error = context.error
        if error is None:
            return

        inv = self._built().invocation("ptb-error", update, context)
        try:
            await self.errors.dispatch(error, inv, self.providers)
        finally:
            await inv.aclose()

    def run(self, *, allowed_updates: Any = Update.ALL_TYPES) -> None:
        """Build and run with long polling. For webhooks, ``build()`` and use PTB."""
        application = self.build()
        application.run_polling(allowed_updates=allowed_updates)


def _check_reachable(
    flat: Sequence[Registration], conv_entries: Sequence[Registration]
) -> None:
    """Fail the build on handlers that could never run.

    PTB stops at the first match in a group, so a second handler for the same
    command -- or for a callback model an earlier handler takes unconditionally
    -- would silently never see an update. Conversation entries come before
    the bot's other handlers in group 0 (see ``_compile``), and take a press
    whenever the caller has no live run of theirs.
    """
    commands: dict[tuple[int, str], str] = {}
    for reg in [*flat, *conv_entries]:
        if reg.kind != "command" or reg.command is None:
            continue
        key = (reg.group, reg.command)
        if key in commands:
            raise ConfigurationError(
                f"/{reg.command} is registered twice ({commands[key]!r} and "
                f"{reg.name!r}); only one of them would ever run"
            )
        commands[key] = reg.name

    takes_all: dict[tuple[int, type[CallbackData]], Registration] = {}
    for reg in [*conv_entries, *flat]:  # the order PTB tries them in
        if reg.kind != "callback" or reg.cb_model is None:
            continue
        key_cb = (reg.group, reg.cb_model)
        if key_cb in takes_all:
            first = takes_all[key_cb]
            model = reg.cb_model.__name__
            if any(first is entry for entry in conv_entries):
                raise ConfigurationError(
                    f"callback handler {reg.name!r} would only see {model} presses "
                    f"from callers mid-conversation: entry {first.name!r} takes "
                    f"every other one, and conversations come first (narrow the "
                    f"entry with when=)"
                )
            raise ConfigurationError(
                f"callback handler {reg.name!r} can never run: {first.name!r} "
                f"already takes every {model} press in its group "
                f"(narrow it with when=)"
            )
        if reg.cb_when is None:
            takes_all[key_cb] = reg


async def _answer_stray(update: Any, context: Any) -> None:
    """Answer a press nothing claimed, so the button doesn't spin to timeout."""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:  # noqa: BLE001
            pass
