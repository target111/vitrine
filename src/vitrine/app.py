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

import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Generic, TypeVar

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    MessageHandler,
    TypeHandler,
)
from telegram.ext import (
    filters as ptb_filters,
)

from . import commands as command_discovery
from .auth import Auth
from .callbacks import CallbackDataError
from .conversations import Conversation
from .dispatch import Dispatch
from .errors import ErrorRegistry
from .exceptions import ConfigurationError
from .injection import Providers
from .media import FileIdCache, InMemoryFileIdCache
from .middleware import Middleware
from .ratelimit import RateLimiter
from .routing import Registration, Router
from .screens import DELIVERY_KEY, NOOP, Delivery
from .workers import WorkerSpec, WorkerSupervisor

logger = logging.getLogger("vitrine.app")

P = TypeVar("P")

ScopeChats = Callable[[], Any] | Sequence[int]


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
        context_type: type[CallbackContext] | None = None,
    ) -> None:
        self.token = token
        self.auth = auth
        self.markdown_version = markdown_version
        self._help_command = help_command
        self._scope_chats = scope_chats or {}
        self._scope_member = scope_member
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
            Application
            .builder()
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
                self.providers, self.errors, self.limiter, self.auth, self._middlewares
            )

        return self._dispatch

    def _attach_delivery(self, delivery: Delivery) -> None:
        self.delivery = delivery
        self._make_dispatch().delivery = delivery
        self.provide_value("delivery", delivery)

    def _wire_handlers(self) -> list[tuple[Any, int]]:
        """Turn all registrations into PTB handlers. Shared by build() and tests."""
        dispatch = self._make_dispatch()
        self._registrations = list(self.router.walk())

        if self._help_command and not any(
            reg.command == "help" for reg in self._registrations if reg.kind == "command"
        ):
            self._registrations.append(self._help_registration())

        wired: list[tuple[Any, int]] = []
        for reg in self._registrations:
            dispatch.validate(reg)
            callback = dispatch.ptb_callback(reg)
            if reg.kind == "command":
                assert reg.command is not None
                handler: Any = CommandHandler(reg.command, callback)
            elif reg.kind == "callback":
                assert reg.cb_model is not None
                handler = CallbackQueryHandler(callback, pattern=_callback_pattern(reg))
            else:
                handler = MessageHandler(
                    reg.filters
                    if reg.filters is not None
                    else ptb_filters.TEXT & ~ptb_filters.COMMAND,
                    callback,
                )
            wired.append((handler, reg.group))

        for conv, middlewares in self.router.walk_conversations():
            wired.append((conv.build(dispatch, middlewares), 0))
        for handler, group in self.router.walk_raw():
            wired.append((handler, group))
        wired.append((
            CallbackQueryHandler(_answer_noop, pattern=lambda d: d == NOOP),
            0,
        ))

        return wired

    # -- auto /help ---------------------------------------------------------------

    def _help_registration(self) -> Registration:
        async def help_command(update: Any, context: Any) -> Any:
            scopes = await self._visible_scopes(update, context)
            return command_discovery.help_screen(self._registrations, scopes)

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
            principal = await self.auth.resolve(inv, self.providers)

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
            await command_discovery.sync_command_menus(
                application.bot, self._registrations, await self._resolve_scope_chats()
            )
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

    async def _resolve_scope_chats(self) -> dict[str, Sequence[int]]:
        resolved: dict[str, Sequence[int]] = {}
        for scope, chats in self._scope_chats.items():
            if callable(chats):
                value = chats()
                if inspect.isawaitable(value):
                    value = await value
                resolved[scope] = list(value)
            else:
                resolved[scope] = list(chats)

        return resolved

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


async def _answer_noop(update: Any, context: Any) -> None:
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:  # noqa: BLE001
            pass


def _callback_pattern(reg: Registration) -> Callable[[object], bool]:
    """Raw-string predicate for PTB; a failed decode still matches so the
    handler can answer with a friendly 'button expired' instead of a dead button."""
    model = reg.cb_model
    when = reg.cb_when
    assert model is not None

    def pattern(data: object) -> bool:
        if not model.matches(data):
            return False
        if when is None:
            return True
        try:
            decoded = model.unpack(data)  # type: ignore[arg-type]
        except CallbackDataError:
            return True

        return bool(when(decoded))

    return pattern
