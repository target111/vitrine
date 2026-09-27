"""Guided multi-step conversations.

State is a dataclass created per run; transitions are string state names
returned by handlers; every exit (finish, cancel, timeout) runs one hook that
receives the reason::

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

A step returns the next state name, :data:`END` (finished), :data:`CANCEL`
(cancelled), ``None`` (stay), or any of those paired with a Screen:
``("qty", Screen(...))``. A bare ``Screen`` renders and stays. Steps, their
screens and the exit hook all run through the same pipeline as every other
handler -- middleware, injection, the principal, guards, error UX -- and the
``state`` parameter injects the run's state object.

A step can be mounted on several states, or on :data:`ANY_STATE` for the
Cancel button that belongs everywhere, and a state can carry its own command::

    @order.state(ANY_STATE, callback=CancelCB)
    async def cancel(state): return CANCEL, Screen(text="Cancelled.")

    @order.state("qty", command="skip")
    async def skip(state): ...

An entry command is a real command: listed in ``/help``, published to the
Telegram menu, and given typed ``Arg[...]`` parameters like
``@router.command`` -- so ``/order ABC 3`` or a ``t.me/bot?start=<payload>``
deep link starts the flow with what it needs. An entry that starts no run
(bad arguments, a failed guard, a throttle, a bare Screen) leaves no trace.

While a caller has a live run, its steps see that caller's updates before
the bot's other message and callback handlers (reply-keyboard buttons, which
are navigation, still come first). ``exclusive=True`` makes starting this
conversation end the caller's other exclusive runs, after the new flow's
first screen has gone out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from telegram import Update
from telegram.ext import BaseHandler

from .callbacks import CallbackData
from .dispatch import Dispatch
from .exceptions import ConfigurationError
from .injection import unresolvable_params
from .middleware import Middleware
from .routing import (
    Registration,
    ptb_handler,
    split_docstring,
    validate_command_name,
)
from .screens import Screen

logger = logging.getLogger("vitrine.conversations")

F = TypeVar("F", bound=Callable[..., Any])


class _Outcome:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


#: a step returns this to finish the run (exit hook gets FINISHED)
END: Any = _Outcome("END")
#: a step returns this to abandon the run (exit hook gets CANCELLED)
CANCEL: Any = _Outcome("CANCEL")

#: state selector meaning "every state this conversation declares"
ANY_STATE = "*"


class ExitReason(Enum):
    FINISHED = "finished"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _exit_reason(outcome: Any) -> ExitReason | None:
    if outcome is END:
        return ExitReason.FINISHED
    if outcome is CANCEL:
        return ExitReason.CANCELLED
    return None


class _Verdict:
    """A step's outcome, as validated inside the pipeline.

    Only the step's own result fills it, so a middleware that answers without
    calling ``call_next`` -- whatever it returns -- means no transition.
    """

    __slots__ = ("outcome",)

    def __init__(self) -> None:
        self.outcome: Any = None


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
        self.edits = edits

    @property
    def is_entry(self) -> bool:
        return not self.states and not self.is_fallback

    def registration(
        self, conv_name: str, middlewares: list[Middleware] | None = None
    ) -> Registration:
        """The one Registration this step implies: /help, menus and dispatch read it."""
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
        if not per_chat and not per_user:
            raise ConfigurationError(
                f"conversation {name!r}: per_chat and per_user can't both be False"
            )
        self.name = name
        self.state_factory = state_factory
        self.timeout = timeout
        self.per_chat = per_chat
        self.per_user = per_user
        #: end other exclusive runs for this caller when this one starts
        self.exclusive = exclusive
        self.frozen = False
        self._steps: list[_Step] = []
        self._exit_hook: Callable[..., Awaitable[Any]] | None = None
        self._handler: ConversationDispatcher | None = None

    def _check_mutable(self) -> None:
        if self.frozen:
            raise ConfigurationError(
                f"conversation {self.name!r} belongs to a bot that is already built"
            )

    # -- declaration -------------------------------------------------------------

    def entry(
        self,
        command: str | None = None,
        callback: type[CallbackData] | None = None,
        when: Callable[[Any], bool] | None = None,
        filters: Any = None,
        *,
        edits: bool = False,
        description: str | None = None,
        scope: str = "default",
        hidden: bool = False,
    ) -> Callable[[F], F]:
        """An entry point: a command, a typed callback button, or a message filter.

        A command entry carries the same menu metadata as ``@router.command``
        and parses the same ``Arg[...]`` parameters; bad arguments get the
        usage line back and the run never starts.
        """
        self._check_mutable()
        if command is None and callback is None and filters is None:
            raise ConfigurationError(
                "conversation entry needs a command, callback, or filters"
            )
        if command is not None:
            validate_command_name(command)

        def register(fn: F) -> F:
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
    ) -> Callable[[F], F]:
        """A step for one or more named states (a text message by default).

        :data:`ANY_STATE` mounts it on every state. ``command="skip"`` makes
        it a ``/skip`` command valid only in those states, so it stays out of
        ``/help``. ``when`` narrows a callback step without swallowing the
        press. ``edits=True`` opts the step in to edited messages.
        """
        self._check_mutable()
        if not names:
            raise ConfigurationError("conversation state needs at least one name")
        if command is not None:
            validate_command_name(command)

        def register(fn: F) -> F:
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

    def cancel(self, command: str = "cancel") -> Callable[[F], F]:
        """A command valid in every state that cancels the run (exit hook: CANCELLED)."""
        self._check_mutable()
        validate_command_name(command)

        def register(fn: F) -> F:
            self._steps.append(_Step(fn, command=command, is_fallback=True))
            return fn

        return register

    def on_exit(self, fn: F) -> F:
        """Exit hook: ``async def hook(state, reason, ...)``; it may return a Screen."""
        self._check_mutable()
        self._exit_hook = fn
        return fn

    def command_registrations(self) -> list[Registration]:
        """Entry commands as discoverable registrations (``/help``, menus).

        Only entries: a state's ``/skip`` or the ``/cancel`` fallback do
        nothing outside a live run.
        """
        return [reg for reg in self.entry_registrations() if reg.kind == "command"]

    def entry_registrations(self) -> list[Registration]:
        """Every entry point -- command, callback or message -- as a registration."""
        return [step.registration(self.name) for step in self._steps if step.is_entry]

    # -- runtime inspection ------------------------------------------------------

    def active(
        self, *, chat_id: int | None = None, user_id: int | None = None
    ) -> tuple[str, Any] | None:
        """The live run for a caller as ``(state_name, state)``, or None."""
        if self._handler is None:
            return None
        key = (chat_id if self.per_chat else None, user_id if self.per_user else None)
        run = self._handler.runs.get(key)
        return (run.state_name, run.state) if run is not None else None

    # -- building -------------------------------------------------------------

    def build(
        self, dispatch: Dispatch, middlewares: list[Middleware]
    ) -> ConversationDispatcher:
        """Compile into the single PTB handler that owns this conversation's runs."""
        state_names = list(
            dict.fromkeys(
                name for step in self._steps for name in step.states if name != ANY_STATE
            )
        )
        self._validate_exit_hook(dispatch)

        entries: list[_Mounted] = []
        states: dict[str, list[_Mounted]] = {name: [] for name in state_names}
        fallbacks: list[_Mounted] = []

        for step in self._steps:
            reg = step.registration(self.name, middlewares)
            dispatch.validate(reg)
            mounted = _Mounted(step, reg, ptb_handler(reg, _unused_callback))
            if step.is_fallback:
                fallbacks.append(mounted)
            elif step.is_entry:
                entries.append(mounted)
            elif ANY_STATE in step.states:
                if not state_names:
                    raise ConfigurationError(
                        f"conversation {self.name!r}: {step.fn.__name__!r} is "
                        f"mounted on every state, but none are declared"
                    )
                for target in state_names:
                    states[target].append(mounted)
            else:
                for target in step.states:
                    states[target].append(mounted)

        if not entries:
            raise ConfigurationError(f"conversation {self.name!r} has no entry points")

        exit_reg = None
        if self._exit_hook is not None:
            exit_reg = Registration(
                kind="message",
                fn=self._exit_hook,
                name=f"{self.name}.on_exit",
                middlewares=middlewares,
            )

        # A Conversation belongs to one bot: building again rebinds it.
        self._handler = ConversationDispatcher(
            self, dispatch, entries, states, fallbacks, exit_reg
        )
        return self._handler

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

    def key(self, update: Any) -> tuple[int | None, int | None] | None:
        """Whose run an update belongs to: (chat, user), per ``per_chat``/``per_user``."""
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        if self.per_chat and chat is None:
            return None
        if self.per_user and user is None:
            return None
        return (
            chat.id if self.per_chat and chat is not None else None,
            user.id if self.per_user and user is not None else None,
        )


async def _unused_callback(update: Any, context: Any) -> None:
    """Steps are matched by their PTB handler but run by the dispatcher."""
    raise AssertionError("conversation steps are run by ConversationDispatcher")


@dataclass
class _Mounted:
    step: _Step
    reg: Registration
    handler: BaseHandler  # used for matching only


@dataclass
class _Run:
    state_name: str
    state: Any
    update: Any  # the latest update, for a timeout's exit hook
    context: Any
    timer: asyncio.Task[None] | None = None


class ConversationDispatcher(BaseHandler[Update, Any, None]):
    """The PTB handler owning one conversation's live runs.

    Without a live run for the caller only entry points match; with one, only
    that state's steps and the fallbacks do.
    """

    def __init__(
        self,
        conv: Conversation,
        dispatch: Dispatch,
        entries: list[_Mounted],
        states: dict[str, list[_Mounted]],
        fallbacks: list[_Mounted],
        exit_reg: Registration | None,
    ) -> None:
        super().__init__(_unused_callback)
        self.conv = conv
        self.dispatch = dispatch
        self.entries = entries
        self.states = states
        self.fallbacks = fallbacks
        self.exit_reg = exit_reg
        self.peers: list[ConversationDispatcher] = []
        self.runs: dict[tuple[int | None, int | None], _Run] = {}

    # -- PTB handler protocol ----------------------------------------------------

    def check_update(self, update: object) -> tuple[_Mounted, object] | None:
        if not isinstance(update, Update):
            return None
        if update.channel_post or update.edited_channel_post:
            return None
        if (
            update.callback_query is not None
            and self.conv.per_chat
            and update.callback_query.message is None
        ):
            return None
        key = self.conv.key(update)
        if key is None:
            return None

        run = self.runs.get(key)
        candidates = self.entries if run is None else self.states[run.state_name]
        if run is not None:
            candidates = [*candidates, *self.fallbacks]
        for mounted in candidates:
            check = mounted.handler.check_update(update)
            if check is not None and check is not False:
                return mounted, check

        return None

    async def handle_update(
        self,
        update: Update,
        application: Any,
        check_result: object,
        context: Any,
    ) -> None:
        assert isinstance(check_result, tuple)
        mounted, inner_check = check_result
        # what the step's own PTB handler would add: context.args, regex matches
        mounted.handler.collect_additional_context(
            context, update, application, inner_check
        )

        key = self.conv.key(update)
        assert key is not None
        if mounted.step.is_entry:
            await self._enter(key, mounted, update, context)
        else:
            await self._step(key, mounted, update, context)

    # -- transitions -------------------------------------------------------------

    async def _enter(
        self,
        key: tuple[int | None, int | None],
        mounted: _Mounted,
        update: Any,
        context: Any,
    ) -> None:
        # The state exists for the entry to fill, but nothing is recorded -- and
        # peers are left alone -- until the result shows a run really started.
        factory = self.conv.state_factory
        state = factory() if factory is not None else None
        verdict = _Verdict()
        await self.dispatch.run(
            mounted.reg,
            update,
            context,
            state=state,
            on_result=self._interpret(update, verdict),
        )
        outcome = verdict.outcome
        if outcome is None:
            return

        if self.conv.exclusive:
            for peer in self.peers:
                await peer.end_run(update, context)

        reason = _exit_reason(outcome)
        if reason is not None:
            await self._exit(update, context, state, reason)
            return

        run = _Run(outcome, state, update, context)
        self.runs[key] = run
        self._arm(key, run)

    async def _step(
        self,
        key: tuple[int | None, int | None],
        mounted: _Mounted,
        update: Any,
        context: Any,
    ) -> None:
        run = self.runs.get(key)
        if run is None:  # timed out between matching and handling
            return
        run.update, run.context = update, context

        verdict = _Verdict()
        await self.dispatch.run(
            mounted.reg,
            update,
            context,
            state=run.state,
            on_result=self._interpret(update, verdict),
        )
        outcome = verdict.outcome
        if self.runs.get(key) is not run:
            return  # ended while the step ran

        reason = ExitReason.CANCELLED if mounted.step.is_fallback else _exit_reason(outcome)
        if reason is not None:
            self._forget(key)
            await self._exit(update, context, run.state, reason)
            return

        if outcome is not None:
            run.state_name = outcome
        self._arm(key, run)

    def _interpret(self, update: Any, verdict: _Verdict) -> Callable[[Any], Awaitable[Any]]:
        """Inside the pipeline: validate a step's result and render its screen."""

        async def interpret(result: Any) -> Any:
            outcome, screen = result, None
            if isinstance(result, tuple) and len(result) == 2:
                outcome, screen = result
            elif isinstance(result, Screen):
                outcome, screen = None, result

            if (
                outcome is not None
                and _exit_reason(outcome) is None
                and not (isinstance(outcome, str) and outcome in self.states)
            ):
                raise ConfigurationError(
                    f"conversation {self.conv.name!r}: step returned unknown state "
                    f"{outcome!r}; declared states are {list(self.states)}"
                )
            if screen is not None:
                await self.dispatch.delivery.render(update, screen)
            verdict.outcome = outcome
            return outcome

        return interpret

    async def end_run(self, update: Any, context: Any) -> bool:
        """Cancel the caller's live run, if any; its exit hook gets CANCELLED."""
        key = self.conv.key(update)
        run = self.runs.get(key) if key is not None else None
        if run is None:
            return False

        assert key is not None
        self._forget(key)
        await self._exit(update, context, run.state, ExitReason.CANCELLED)
        return True

    async def _exit(
        self, update: Any, context: Any, state: Any, reason: ExitReason
    ) -> None:
        if self.exit_reg is None:
            return
        await self.dispatch.run(
            self.exit_reg,
            update,
            context,
            state=state,
            extras={"reason": reason},
            answer_query=False,
        )

    # -- timeouts ----------------------------------------------------------------

    def _arm(self, key: tuple[int | None, int | None], run: _Run) -> None:
        """(Re)start the run's inactivity timer; any handled update resets it."""
        if run.timer is not None:
            run.timer.cancel()
            run.timer = None
        if self.conv.timeout is not None:
            run.timer = asyncio.get_running_loop().create_task(
                self._expire(key, run), name=f"vitrine-conv-timeout-{self.conv.name}"
            )

    async def _expire(self, key: tuple[int | None, int | None], run: _Run) -> None:
        assert self.conv.timeout is not None
        await asyncio.sleep(self.conv.timeout)
        if self.runs.get(key) is not run:
            return
        run.timer = None  # this task is the timer: don't cancel it
        del self.runs[key]
        await self._exit(run.update, run.context, run.state, ExitReason.TIMEOUT)

    def _forget(self, key: tuple[int | None, int | None]) -> None:
        run = self.runs.pop(key, None)
        if run is not None and run.timer is not None:
            run.timer.cancel()

    def close(self) -> None:
        """Cancel every pending timeout (bot shutdown); runs are in-memory only."""
        for run in self.runs.values():
            if run.timer is not None:
                run.timer.cancel()
        self.runs.clear()
