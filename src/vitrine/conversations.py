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

A step can be mounted on several states at once -- or on :data:`ANY_STATE`,
for the Cancel button that belongs everywhere -- and a state can carry its own
command::

    @order.state(ANY_STATE, callback=CancelCB)
    async def cancel(state): return END, Screen(text="Cancelled.")

    @order.state("qty", command="skip")
    async def skip(state): ...

An entry command is a real command: it is listed in ``/help`` and published to
the Telegram command menu, with the same ``description``/``scope``/``hidden``
metadata as ``@router.command`` -- and with ``args=True``, the same typed
command arguments, so ``/order ABC 3`` (or a ``t.me/bot?start=<payload>`` deep
link) starts the flow with what it needs already in hand. ``exclusive=True``
makes starting this conversation end the caller's other exclusive runs, so a
half-finished flow can't keep matching messages meant for the new one; peers
end only once a run really starts, and only after the entry's screen renders.

An entry that starts no run leaves no trace: refused arguments, a failed
guard, a throttle, or a handler returning a bare ``Screen`` neither end the
caller's other runs nor leave a draft state behind. Returning :data:`END`
counts as started-and-finished.
"""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable, Coroutine
from enum import Enum
from typing import Any

from telegram import Update
from telegram.ext import ConversationHandler, TypeHandler
from telegram.warnings import PTBUserWarning

from .callbacks import CallbackData
from .dispatch import Dispatch
from .exceptions import ConfigurationError
from .injection import resolve_kwargs, unresolvable_params
from .middleware import Middleware
from .routing import (
    Registration,
    ptb_handler,
    split_docstring,
    validate_command_name,
)
from .screens import Screen

#: sentinel a handler returns to finish the conversation
END = ConversationHandler.END

#: state selector meaning "every state this conversation declares" -- for the
#: handler that belongs everywhere, typically a Cancel button
ANY_STATE = "*"

#: distinguishes "no state passed" from an explicit ``state=None``
_UNSET = object()

#: PTB internals :meth:`Conversation.end_run` needs to end a peer's live run.
#: Private, so no version bound protects them -- see ``_check_exclusive_support``.
_PTB_INTERNALS = (
    "_get_key",
    "_conversations",
    "_update_state",
    "timeout_jobs",
    "_timeout_jobs_lock",
)


class ExitReason(Enum):
    FINISHED = "finished"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class _Step:
    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        states: tuple[str, ...] = (),  # empty -> entry point (or fallback)
        command: str | None = None,
        callback: type[CallbackData] | None = None,
        when: Callable[[Any], bool] | None = None,
        filters: Any = None,
        is_fallback: bool = False,
        description: str = "",
        scope: str = "default",
        hidden: bool = False,
        args: bool = False,
        edits: bool = False,
    ) -> None:
        self.fn = fn
        self.states = states
        self.command = command
        self.callback = callback
        self.when = when
        self.filters = filters
        self.is_fallback = is_fallback
        self.description = description
        self.scope = scope
        self.hidden = hidden
        self.args = args
        self.edits = edits

    @property
    def is_entry(self) -> bool:
        return not self.states and not self.is_fallback

    def registration(
        self, conv_name: str, middlewares: list[Middleware] | None = None
    ) -> Registration:
        """The one Registration this step implies.

        ``/help``, the command menus, and the wired PTB handler all read the
        same record. ``kind`` states the transport -- a command entry without
        ``args=True`` is still a command; whether its extra parameters are
        parsed arguments or injected is ``args``'s business alone.
        """
        return Registration(
            kind="command"
            if self.command is not None
            else "callback"
            if self.callback is not None
            else "message",
            fn=self.fn,
            name=f"{conv_name}.{self.fn.__name__}",
            command=self.command,
            description=self.description,
            scope=self.scope,
            hidden=self.hidden,
            cb_model=self.callback,
            cb_when=self.when,
            filters=self.filters,
            args=self.args,
            edits=self.edits,
            middlewares=middlewares or [],
        )


class Conversation:
    def __init__(
        self,
        name: str,
        state_factory: Callable[[], Any] | None = None,
        *,
        timeout: float | None = None,
        per_chat: bool = True,
        per_user: bool = True,
        exclusive: bool = False,
    ) -> None:
        self.name = name
        self.state_factory = state_factory
        self.timeout = timeout
        self.per_chat = per_chat
        self.per_user = per_user
        #: end other exclusive runs for this caller when this one starts, so a
        #: half-finished flow can't keep swallowing answers meant for the new one
        self.exclusive = exclusive
        self._steps: list[_Step] = []
        self._exit_hook: Callable[..., Awaitable[Any]] | None = None
        self._handler: ConversationHandler | None = None
        self._peers: list[Conversation] = []

    # -- declaration -------------------------------------------------------------

    def entry(
        self,
        command: str | None = None,
        callback: type[CallbackData] | None = None,
        when: Callable[[Any], bool] | None = None,
        filters: Any = None,
        *,
        args: bool = False,
        edits: bool = False,
        description: str | None = None,
        scope: str = "default",
        hidden: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """An entry point: a command, a typed callback button, or a message filter.

        A command entry carries the same menu metadata as ``@router.command``:
        it is listed in ``/help`` and published to the Telegram command menu of
        its ``scope`` unless ``hidden``.

        ``args=True`` gives a command entry the same typed arguments a
        ``@router.command`` handler gets, so a flow can start with what it
        needs already in hand (``/order ABC 3``, or a ``t.me/bot?start=<x>``
        deep link). Bad or missing arguments get the usage line back and the
        run never starts -- entered with its arguments or not at all. Off by
        default because an entry's extra parameters are injected, as they
        always were, and a flow normally asks for what it needs one state at
        a time.
        """
        if command is None and callback is None and filters is None:
            raise ConfigurationError(
                "conversation entry needs a command, callback, or filters"
            )
        if args and command is None:
            raise ConfigurationError(
                "conversation entry: args=True needs a command; there is no "
                "command line to read arguments from"
            )
        if command is not None:
            validate_command_name(command)

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            desc = split_docstring(fn)[0] if description is None else description

            self._steps.append(
                _Step(
                    fn,
                    command=command,
                    callback=callback,
                    when=when,
                    filters=filters,
                    description=desc,
                    scope=scope,
                    hidden=hidden,
                    args=args,
                    edits=edits,
                )
            )
            return fn

        return register

    def state(
        self,
        *names: str,
        command: str | None = None,
        callback: type[CallbackData] | None = None,
        when: Callable[[Any], bool] | None = None,
        filters: Any = None,
        edits: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """A handler for one or more named states (text message by default).

        Pass several names -- or :data:`ANY_STATE` for all of them -- to mount
        the same handler on each, which is what a Cancel button wants.
        ``command="skip"`` makes the step a ``/skip`` command instead: valid
        only while the run sits in that state, so it stays out of ``/help``.
        ``when`` narrows the match on decoded callback data without swallowing
        the press: a rejected button stays available to a sibling step on the
        same model. ``edits=True`` opts the step in to edited messages.
        """
        if not names:
            raise ConfigurationError("conversation state needs at least one name")
        if command is not None:
            validate_command_name(command)

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._steps.append(
                _Step(
                    fn,
                    states=names,
                    command=command,
                    callback=callback,
                    when=when,
                    filters=filters,
                    edits=edits,
                )
            )
            return fn

        return register

    def cancel(
        self, command: str = "cancel"
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """A fallback command that cancels the run (exit hook gets CANCELLED)."""
        validate_command_name(command)

        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._steps.append(_Step(fn, command=command, is_fallback=True))
            return fn

        return register

    def command_registrations(self) -> list[Registration]:
        """Entry commands as discoverable registrations (``/help``, menus).

        Only entry points: a state's ``/skip`` or the ``/cancel`` fallback do
        nothing outside a live run, so listing them would be noise. The
        ``args`` flag rides along so ``/help <command>`` knows whether the
        entry's extra parameters are arguments or injected.
        """
        return [
            step.registration(self.name)
            for step in self._steps
            if step.is_entry and step.command is not None
        ]

    def on_exit(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Cleanup hook: ``async def hook(state, reason, ...services)``."""
        self._exit_hook = fn
        return fn

    # -- state storage -------------------------------------------------------------

    def _store(self, update: Any, context: Any) -> dict[str, Any]:
        # mirror PTB's conversation key: per-chat state lives on the chat,
        # otherwise it must follow the user across chats
        preferred = (
            ("chat_data", "user_data") if self.per_chat else ("user_data", "chat_data")
        )
        for attr in preferred:
            holder = getattr(context, attr, None)
            if holder is not None:
                return holder

        raise ConfigurationError("context has neither chat_data nor user_data")

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

    def build(
        self, dispatch: Dispatch, middlewares: list[Middleware]
    ) -> ConversationHandler:
        # dict, not set: the declaration order of the states is preserved
        state_names = dict.fromkeys(
            name for step in self._steps for name in step.states if name != ANY_STATE
        )
        self._validate_exit_hook(dispatch)

        known_states = set(state_names)
        entry_points: list[Any] = []
        states: dict[Any, list[Any]] = {name: [] for name in state_names}
        fallbacks: list[Any] = []

        for step in self._steps:
            reg = step.registration(self.name, middlewares)
            # Same build-time check every other handler gets: a step that
            # declares an unknown parameter fails here, not on the update that
            # finally reaches it.
            dispatch.validate(reg)
            callback = self._make_callback(dispatch, reg, step, known_states)
            handler = ptb_handler(reg, callback)
            if step.is_fallback:
                fallbacks.append(handler)
            elif step.is_entry:
                entry_points.append(handler)
            elif ANY_STATE in step.states:
                if not state_names:
                    raise ConfigurationError(
                        f"conversation {self.name!r}: {step.fn.__name__!r} is "
                        f"mounted on every state, but none are declared"
                    )
                for target in state_names:
                    states[target].append(handler)
            else:
                # every declared name is a state by construction of state_names
                for target in step.states:
                    states[target].append(handler)

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
            # Kept so an exclusive peer can end this conversation's live run.
            # A Conversation belongs to one bot: building it again (a second
            # Bot, or a rebuild in tests) rebinds it to the newer handler.
            self._handler = ConversationHandler(
                entry_points=entry_points,
                states=states,
                fallbacks=fallbacks,
                conversation_timeout=self.timeout,
                name=self.name,
                per_chat=self.per_chat,
                per_user=self.per_user,
            )

        if self.exclusive:
            self._check_exclusive_support(self._handler)

        return self._handler

    def _check_exclusive_support(self, handler: ConversationHandler) -> None:
        """Fail the build, not an update, if PTB moved what ``end_run`` reaches for.

        Only ``exclusive=True`` touches these, so an app that does not use it
        keeps building against a PTB this check would reject. Presence is all
        this can prove: PTB could keep the names and change what they mean, and
        only the exclusivity tests would notice.
        """
        missing = [name for name in _PTB_INTERNALS if not hasattr(handler, name)]
        if missing:
            raise ConfigurationError(
                f"conversation {self.name!r}: exclusive=True ends a peer's live "
                f"run through python-telegram-bot internals {missing}, which "
                f"this version no longer exposes. Pin an older PTB or drop "
                f"exclusive=True until vitrine catches up."
            )

    def _validate_exit_hook(self, dispatch: Dispatch) -> None:
        if self._exit_hook is None:
            return

        extra = {"reason"} | ({dispatch.auth.name} if dispatch.auth else set())
        bad = unresolvable_params(self._exit_hook, dispatch.providers, extra_names=extra)
        if bad:
            raise ConfigurationError(
                f"conversation {self.name!r}: exit hook declares parameter(s) "
                f"{bad} that nothing can supply"
            )

    # -- exclusivity -------------------------------------------------------------

    def link_peers(self, peers: list[Conversation]) -> None:
        """Tell this conversation which other runs it must end when it starts."""
        self._peers = [peer for peer in peers if peer is not self]

    async def end_run(self, dispatch: Dispatch, update: Any, context: Any) -> bool:
        """Cancel this conversation's live run for ``update``'s caller.

        Returns whether there was one. PTB owns which run is live, so the key
        and the bookkeeping come from its handler: drop the state, then remove
        the pending timeout job the way ``handle_update`` does -- otherwise it
        fires later and reports a timeout for a run that is already gone.
        """
        handler = self._handler
        if handler is None:
            return False

        try:
            key = handler._get_key(update)
        except RuntimeError:  # an update with no chat/user has no run
            return False

        if key not in handler._conversations:
            return False

        handler._update_state(END, key)
        async with handler._timeout_jobs_lock:
            job = handler.timeout_jobs.pop(key, None)
        if job is not None:
            job.schedule_removal()

        await self._run_exit(dispatch, update, context, ExitReason.CANCELLED)
        self._clear_state(update, context)
        return True

    def _make_callback(
        self,
        dispatch: Dispatch,
        reg: Registration,
        step: _Step,
        state_names: set[str],
    ) -> Callable[[Any, Any], Coroutine[Any, Any, Any]]:
        if step.is_entry:
            return self._make_entry_callback(dispatch, reg, state_names)

        async def handle(update: Any, context: Any) -> Any:
            state = self._get_state(update, context)
            result = await dispatch.run(reg, update, context, state=state)

            return await self._apply_result(
                dispatch,
                update,
                context,
                result,
                state_names,
                end_reason=ExitReason.CANCELLED
                if step.is_fallback
                else ExitReason.FINISHED,
                force_end=step.is_fallback,
            )

        return handle

    def _make_entry_callback(
        self,
        dispatch: Dispatch,
        reg: Registration,
        state_names: set[str],
    ) -> Callable[[Any, Any], Coroutine[Any, Any, Any]]:
        async def handle(update: Any, context: Any) -> Any:
            # The state object exists for the handler to fill, but it is not
            # stored -- and peers are not touched -- until the result proves a
            # run actually started. Refused arguments, a failed guard, a
            # throttle, or a bare Screen must leave no trace: no draft state,
            # and the caller's *other* exclusive runs keep going.
            state = self.state_factory() if self.state_factory is not None else None
            result = await dispatch.run(reg, update, context, state=state)
            next_state, screen = self._split_result(result, state_names)

            if screen is not None and dispatch.delivery is not None:
                await dispatch.delivery.render(update, screen)

            if next_state is None:
                return None  # no run started; PTB records nothing

            # END did start and finish, so it counts as a start. Peers end
            # only now, *after* the entry's screen went out: a peer's goodbye
            # follows the new flow's hello rather than preceding it.
            if self.exclusive:
                for peer in self._peers:
                    await peer.end_run(dispatch, update, context)

            if next_state == END:
                await self._run_exit(
                    dispatch, update, context, ExitReason.FINISHED, state=state
                )
                return END

            self._set_state(update, context, state)
            return next_state

        return handle

    def _split_result(
        self, result: Any, state_names: set[str]
    ) -> tuple[Any, Screen | None]:
        """Normalize a handler result to ``(next_state | END | None, screen)``."""
        screen: Screen | None = None
        next_state: Any = result
        if isinstance(result, tuple) and len(result) == 2:
            next_state, screen = result
        elif isinstance(result, Screen):
            next_state = None  # already rendered by the pipeline core

        if next_state is not None and next_state != END and next_state not in state_names:
            raise ConfigurationError(
                f"conversation {self.name!r}: handler returned unknown state {next_state!r}"
            )

        return next_state, screen

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
        next_state, screen = self._split_result(result, state_names)

        if screen is not None and dispatch.delivery is not None:
            await dispatch.delivery.render(update, screen)

        if force_end or next_state == END:
            await self._run_exit(dispatch, update, context, end_reason)
            self._clear_state(update, context)
            return END

        return next_state  # a state name, or None to stay put

    def _make_timeout_callback(
        self, dispatch: Dispatch
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        async def on_timeout(update: Any, context: Any) -> Any:
            await self._run_exit(dispatch, update, context, ExitReason.TIMEOUT)
            self._clear_state(update, context)
            return END

        return on_timeout

    async def _run_exit(
        self,
        dispatch: Dispatch,
        update: Any,
        context: Any,
        reason: ExitReason,
        *,
        state: Any = _UNSET,
    ) -> None:
        if self._exit_hook is None:
            return

        inv = dispatch.invocation(f"{self.name}.on_exit", update, context)
        # An entry that started and finished in one step never stored its
        # state, so the caller hands it over instead of the store.
        inv.state = self._get_state(update, context) if state is _UNSET else state
        inv.extras["reason"] = reason
        try:
            kwargs = await resolve_kwargs(self._exit_hook, inv, dispatch.providers)
            await self._exit_hook(**kwargs)
        finally:
            await inv.aclose()
