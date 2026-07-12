"""Guided multi-step conversations.

State is a dataclass created per run; transitions are string state names
returned by handlers; exits (finish, cancel, timeout) run one cleanup hook
that receives the reason::

    @dataclass
    class OrderState:
        item: str | None = None
        qty: int = 0

    order = Conversation("order", OrderState, timeout=120)

    @order.entry(command="order")
    async def start(state: OrderState, update):
        return "item", Screen(text="What would you like?")

    @order.state("item")
    async def got_item(state: OrderState, update, order_service):
        state.item = update.effective_message.text
        return "qty", Screen(text="How many?")

    @order.state("qty")
    async def got_qty(state: OrderState, update):
        state.qty = int(update.effective_message.text)
        return END, Screen(text="Done!")

    @order.on_exit
    async def cleanup(state, reason, order_service):
        if reason is not ExitReason.FINISHED:
            await order_service.release_hold(state.item)

    router.conversation(order)

Handlers return the next state name, :data:`END`, ``None`` (stay), or a
``(next_state, Screen)`` tuple. Conversation steps go through the same
pipeline as every other handler: middleware, injection, the resolved
principal, and guards all work; the ``state`` parameter injects the run's
state object. Built on PTB's ``ConversationHandler``.
"""

from __future__ import annotations

import warnings
from enum import Enum
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters as ptb_filters,
)

from .callbacks import CallbackData
from .dispatch import Dispatch
from .exceptions import ConfigurationError
from .injection import resolve_kwargs
from .middleware import Middleware
from .routing import Registration
from .screens import Screen

#: sentinel a handler returns to finish the conversation
END = ConversationHandler.END


class ExitReason(Enum):
    FINISHED = "finished"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class _Step:
    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        state: str | None,  # None -> entry point
        command: str | None = None,
        callback: type[CallbackData] | None = None,
        when: Callable[[Any], bool] | None = None,
        filters: Any = None,
        is_fallback: bool = False,
    ) -> None:
        self.fn = fn
        self.state = state
        self.command = command
        self.callback = callback
        self.when = when
        self.filters = filters
        self.is_fallback = is_fallback


class Conversation:
    def __init__(
        self,
        name: str,
        state_factory: Callable[[], Any] | None = None,
        *,
        timeout: float | None = None,
        per_chat: bool = True,
        per_user: bool = True,
    ) -> None:
        self.name = name
        self.state_factory = state_factory
        self.timeout = timeout
        self.per_chat = per_chat
        self.per_user = per_user
        self._steps: list[_Step] = []
        self._exit_hook: Callable[..., Awaitable[Any]] | None = None

    # -- declaration -------------------------------------------------------------

    def entry(
        self,
        command: str | None = None,
        callback: type[CallbackData] | None = None,
        when: Callable[[Any], bool] | None = None,
        filters: Any = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """An entry point: a command, a typed callback button, or a message filter."""
        if command is None and callback is None and filters is None:
            raise ConfigurationError("conversation entry needs a command, callback, or filters")

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._steps.append(
                _Step(fn, state=None, command=command, callback=callback, when=when, filters=filters)
            )
            return fn

        return register

    def state(
        self,
        name: str,
        *,
        callback: type[CallbackData] | None = None,
        when: Callable[[Any], bool] | None = None,
        filters: Any = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """A handler for one named state (text message by default)."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._steps.append(
                _Step(fn, state=name, callback=callback, when=when, filters=filters)
            )
            return fn

        return register

    def cancel(
        self, command: str = "cancel"
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """A fallback command that cancels the run (exit hook gets CANCELLED)."""

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._steps.append(_Step(fn, state=None, command=command, is_fallback=True))
            return fn

        return register

    def on_exit(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Cleanup hook: ``async def hook(state, reason, ...services)``."""
        self._exit_hook = fn
        return fn

    # -- state storage -------------------------------------------------------------

    def _store(self, update: Any, context: Any) -> dict[str, Any]:
        holder = getattr(context, "chat_data", None)
        if holder is None:
            holder = getattr(context, "user_data", None)
        if holder is None:
            raise ConfigurationError("context has neither chat_data nor user_data")

        return holder

    def _key(self, update: Any) -> str:
        user = getattr(update, "effective_user", None)
        uid = getattr(user, "id", 0) if self.per_user else 0
        return f"__vitrine_conv:{self.name}:{uid}"

    def _get_state(self, update: Any, context: Any) -> Any:
        return self._store(update, context).get(self._key(update))

    def _set_state(self, update: Any, context: Any, state: Any) -> None:
        self._store(update, context)[self._key(update)] = state

    def _clear_state(self, update: Any, context: Any) -> Any:
        return self._store(update, context).pop(self._key(update), None)

    # -- building -------------------------------------------------------------

    def build(self, dispatch: Dispatch, middlewares: list[Middleware]) -> ConversationHandler:
        state_names = {step.state for step in self._steps if step.state is not None}

        entry_points: list[Any] = []
        states: dict[Any, list[Any]] = {name: [] for name in state_names}
        fallbacks: list[Any] = []

        for step in self._steps:
            reg = Registration(
                kind="callback" if step.callback else "message",
                fn=step.fn,
                name=f"{self.name}.{step.fn.__name__}",
                cb_model=step.callback,
                cb_when=step.when,
                middlewares=middlewares,
            )
            callback = self._make_callback(dispatch, reg, step, state_names)
            handler = self._ptb_handler(step, callback)
            if step.is_fallback:
                fallbacks.append(handler)
            elif step.state is None:
                entry_points.append(handler)
            else:
                states[step.state].append(handler)

        if self.timeout is not None:
            states[ConversationHandler.TIMEOUT] = [
                TypeHandler(Update, self._make_timeout_callback(dispatch))
            ]

        if not entry_points:
            raise ConfigurationError(f"conversation {self.name!r} has no entry points")

        with warnings.catch_warnings():
            # mixing message and callback handlers across states is the whole
            # point here; PTB's per_message nag does not apply
            warnings.filterwarnings("ignore", category=PTBUserWarning)
            return ConversationHandler(
                entry_points=entry_points,
                states=states,
                fallbacks=fallbacks,
                conversation_timeout=self.timeout,
                name=self.name,
                per_chat=self.per_chat,
                per_user=self.per_user,
            )

    def _ptb_handler(self, step: _Step, callback: Callable[..., Awaitable[Any]]) -> Any:
        if step.command is not None:
            return CommandHandler(step.command, callback)

        if step.callback is not None:
            return CallbackQueryHandler(callback, pattern=step.callback.matches)

        message_filters = step.filters
        if message_filters is None:
            message_filters = ptb_filters.TEXT & ~ptb_filters.COMMAND

        return MessageHandler(message_filters, callback)

    def _make_callback(
        self,
        dispatch: Dispatch,
        reg: Registration,
        step: _Step,
        state_names: set[str],
    ) -> Callable[[Any, Any], Awaitable[Any]]:
        is_entry = step.state is None and not step.is_fallback

        async def handle(update: Any, context: Any) -> Any:
            if is_entry:
                state = self.state_factory() if self.state_factory is not None else None
                self._set_state(update, context, state)
            else:
                state = self._get_state(update, context)

            result = await dispatch.run(reg, update, context, state=state)

            return await self._apply_result(
                dispatch, update, context, result, state_names,
                end_reason=ExitReason.CANCELLED if step.is_fallback else ExitReason.FINISHED,
                force_end=step.is_fallback,
            )

        return handle

    async def _apply_result(
        self,
        dispatch: Dispatch,
        update: Any,
        context: Any,
        result: Any,
        state_names: set[str],
        *,
        end_reason: ExitReason,
        force_end: bool,
    ) -> Any:
        screen: Screen | None = None
        next_state: Any = result
        if isinstance(result, tuple) and len(result) == 2:
            next_state, screen = result
        elif isinstance(result, Screen):
            next_state = None  # already rendered by the pipeline core

        if screen is not None and dispatch.delivery is not None:
            await dispatch.delivery.render(update, screen)

        if force_end or next_state == END:
            await self._run_exit(dispatch, update, context, end_reason)
            self._clear_state(update, context)
            return END

        if next_state is None:
            return None  # stay in the current state

        if next_state not in state_names:
            raise ConfigurationError(
                f"conversation {self.name!r}: handler returned unknown state {next_state!r}"
            )

        return next_state

    def _make_timeout_callback(self, dispatch: Dispatch) -> Callable[..., Awaitable[Any]]:
        async def on_timeout(update: Any, context: Any) -> Any:
            await self._run_exit(dispatch, update, context, ExitReason.TIMEOUT)
            self._clear_state(update, context)
            return END

        return on_timeout

    async def _run_exit(
        self, dispatch: Dispatch, update: Any, context: Any, reason: ExitReason
    ) -> None:
        if self._exit_hook is None:
            return

        inv = dispatch.invocation(f"{self.name}.on_exit", update, context)
        inv.state = self._get_state(update, context)
        inv.extras["reason"] = reason
        try:
            kwargs = await resolve_kwargs(self._exit_hook, inv, dispatch.providers)
            await self._exit_hook(**kwargs)
        finally:
            await inv.aclose()
