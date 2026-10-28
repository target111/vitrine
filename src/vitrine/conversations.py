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

A run belongs to one user in one chat, and a caller has at most one live run
per chat. While it lives, its current state's steps see the caller's updates
first -- before any entry point, and before the bot's other message and
callback handlers (reply-keyboard buttons, which are navigation, still come
first). An update the run doesn't take can still start a conversation:
another one, or the same one again from the top. The new run replaces the old
one after its first screen has gone out; the old run's exit hook gets
CANCELLED, but a Screen it returns is dropped -- the new flow's screen is the
answer to this update.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from telegram import Update
from telegram.ext import BaseHandler

from .callbacks import CallbackData
from .dispatch import Dispatch
from .exceptions import ConfigurationError
from .middleware import Middleware
from .routing import (
    Registration,
    ptb_handler,
    split_docstring,
    validate_command_name,
)
from .screens import Screen

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
    ) -> None:
        self.name = name
        self.state_factory = state_factory
        self.timeout = timeout
        self.frozen = False
        self._steps: list[_Step] = []
        self._exit_hook: Callable[..., Awaitable[Any]] | None = None
        self._flow: _Flow | None = None

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

    def entry_registrations(self) -> list[Registration]:
        """Every entry point -- command, callback or message -- as a registration.

        Only entries: a state's ``/skip`` or the ``/cancel`` command do nothing
        outside a live run, so they stay out of ``/help`` and the menus.
        """
        return [step.registration(self.name) for step in self._steps if step.is_entry]

    # -- runtime inspection ------------------------------------------------------

    def active(self, *, chat_id: int, user_id: int) -> tuple[str, Any] | None:
        """The caller's live run in this conversation as ``(state_name, state)``."""
        flow = self._flow
        if flow is None or flow.dispatcher is None:
            return None
        run = flow.dispatcher.runs.get((chat_id, user_id))
        if run is None or run.flow is not flow:
            return None
        return run.state_name, run.state

    # -- building -------------------------------------------------------------

    def build(self, dispatch: Dispatch, middlewares: list[Middleware]) -> _Flow:
        """Compile into what the bot's ConversationDispatcher matches and runs."""
        state_names = list(
            dict.fromkeys(
                name for step in self._steps for name in step.states if name != ANY_STATE
            )
        )

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
                kind="hook",
                fn=self._exit_hook,
                name=f"{self.name}.on_exit",
                middlewares=middlewares,
            )
            dispatch.validate(exit_reg, extra_names={"reason"})

        # A Conversation belongs to one bot: building again rebinds it.
        self._flow = _Flow(self, entries, states, fallbacks, exit_reg)
        return self._flow


def _key(update: Any) -> tuple[int, int] | None:
    """Whose run an update belongs to: (chat, user)."""
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    if chat is None or user is None:
        return None
    return chat.id, user.id


async def _ignore_result(result: Any) -> Any:
    return result


async def _unused_callback(update: Any, context: Any) -> None:
    """Steps are matched by their PTB handler but run by the dispatcher."""
    raise AssertionError("conversation steps are run by ConversationDispatcher")


@dataclass
class _Mounted:
    step: _Step
    reg: Registration
    handler: BaseHandler  # used for matching only


@dataclass(eq=False)
class _Flow:
    """One conversation, compiled: its entries, each state's steps, its exit hook."""

    conv: Conversation
    entries: list[_Mounted]
    states: dict[str, list[_Mounted]]
    fallbacks: list[_Mounted]
    exit_reg: Registration | None
    #: the dispatcher running this flow, once the bot mounts it
    dispatcher: ConversationDispatcher | None = None


@dataclass
class _Run:
    flow: _Flow
    state_name: str
    state: Any
    update: Any  # the latest update, for a timeout's exit hook
    context: Any
    timer: asyncio.Task[None] | None = None


class ConversationDispatcher(BaseHandler[Update, Any, None]):
    """The one PTB handler running every conversation's live runs.

    A caller has at most one run per chat. Its current state's steps and its
    cancel commands are tried first, then every conversation's entry points --
    its own included, so re-entering restarts it.
    """

    def __init__(self, dispatch: Dispatch, flows: list[_Flow]) -> None:
        super().__init__(_unused_callback)
        self.dispatch = dispatch
        self.flows = flows
        self.runs: dict[tuple[int, int], _Run] = {}
        #: timers running an exit hook: out of ``runs``, still cancellable by close()
        self._expiring: set[asyncio.Task[None]] = set()
        for flow in flows:
            flow.dispatcher = self

    # -- PTB handler protocol ----------------------------------------------------

    def check_update(
        self, update: object
    ) -> tuple[tuple[int, int], _Flow, _Mounted, object] | None:
        if not isinstance(update, Update):
            return None
        if update.channel_post or update.edited_channel_post:
            return None
        if update.callback_query is not None and update.callback_query.message is None:
            return None
        key = _key(update)
        if key is None:
            return None

        candidates: list[tuple[_Flow, _Mounted]] = []
        run = self.runs.get(key)
        if run is not None:
            steps = [*run.flow.states[run.state_name], *run.flow.fallbacks]
            candidates += [(run.flow, mounted) for mounted in steps]
        candidates += [(flow, mounted) for flow in self.flows for mounted in flow.entries]

        for flow, mounted in candidates:
            check = mounted.handler.check_update(update)
            if check is not None and check is not False:
                return key, flow, mounted, check

        return None

    async def handle_update(
        self,
        update: Update,
        application: Any,
        check_result: object,
        context: Any,
    ) -> None:
        assert isinstance(check_result, tuple)
        key, flow, mounted, inner_check = check_result
        # what the step's own PTB handler would add: context.args, regex matches
        mounted.handler.collect_additional_context(
            context, update, application, inner_check
        )

        if mounted.step.is_entry:
            await self._enter(key, flow, mounted, update, context)
        else:
            await self._step(key, flow, mounted, update, context)

    # -- transitions -------------------------------------------------------------

    async def _enter(
        self,
        key: tuple[int, int],
        flow: _Flow,
        mounted: _Mounted,
        update: Any,
        context: Any,
    ) -> None:
        # The state exists for the entry to fill, but nothing is recorded -- and
        # the caller's current run is left alone -- until the result shows a run
        # really started.
        factory = flow.conv.state_factory
        state = factory() if factory is not None else None
        # None too when a middleware answered without calling the step
        outcome = await self.dispatch.run(
            mounted.reg,
            update,
            context,
            state=state,
            on_result=self._interpret(flow, update),
        )
        if outcome is None:
            return

        await self._end_replaced(key, update, context)

        reason = _exit_reason(outcome)
        if reason is not None:
            await self._exit(flow, update, context, state, reason)
            return

        run = _Run(flow, outcome, state, update, context)
        self.runs[key] = run
        self._arm(key, run)

    async def _step(
        self,
        key: tuple[int, int],
        flow: _Flow,
        mounted: _Mounted,
        update: Any,
        context: Any,
    ) -> None:
        run = self.runs.get(key)
        if run is None or run.flow is not flow:  # ended between matching and handling
            return
        run.update, run.context = update, context

        # A slow step mustn't time out under itself: the clock restarts after it.
        self._disarm(run)
        try:
            # None too when a middleware answered without calling the step
            outcome = await self.dispatch.run(
                mounted.reg,
                update,
                context,
                state=run.state,
                on_result=self._interpret(flow, update),
            )
        except BaseException:
            if self.runs.get(key) is run:
                self._arm(key, run)  # don't leave the run unable to expire
            raise
        if self.runs.get(key) is not run:
            return  # ended while the step ran

        reason = ExitReason.CANCELLED if mounted.step.is_fallback else _exit_reason(outcome)
        if reason is not None:
            self._forget(key)
            await self._exit(flow, update, context, run.state, reason)
            return

        if outcome is not None:
            run.state_name = outcome
        self._arm(key, run)

    def _interpret(self, flow: _Flow, update: Any) -> Callable[[Any], Awaitable[Any]]:
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
                and not (isinstance(outcome, str) and outcome in flow.states)
            ):
                raise ConfigurationError(
                    f"conversation {flow.conv.name!r}: step returned unknown state "
                    f"{outcome!r}; declared states are {list(flow.states)}"
                )
            if screen is not None:
                await self.dispatch.delivery.render(update, screen)
            return outcome

        return interpret

    async def _end_replaced(self, key: tuple[int, int], update: Any, context: Any) -> None:
        """Cancel the caller's run that a new one replaces, in any conversation.

        Its exit hook gets CANCELLED, but a Screen it returns is dropped: the
        update already got the new flow's first screen.
        """
        run = self.runs.get(key)
        if run is None:
            return

        self._forget(key)
        await self._exit(
            run.flow, update, context, run.state, ExitReason.CANCELLED, render=False
        )

    async def _exit(
        self,
        flow: _Flow,
        update: Any,
        context: Any,
        state: Any,
        reason: ExitReason,
        *,
        render: bool = True,
    ) -> None:
        if flow.exit_reg is None:
            return
        await self.dispatch.run(
            flow.exit_reg,
            update,
            context,
            state=state,
            extras={"reason": reason},
            on_result=None if render else _ignore_result,
            answer_query=False,
        )

    # -- timeouts ----------------------------------------------------------------

    def _arm(self, key: tuple[int, int], run: _Run) -> None:
        """(Re)start the run's inactivity timer; any handled update resets it."""
        self._disarm(run)
        if run.flow.conv.timeout is not None:
            run.timer = asyncio.get_running_loop().create_task(
                self._expire(key, run), name=f"vitrine-conv-timeout-{run.flow.conv.name}"
            )

    @staticmethod
    def _disarm(run: _Run) -> None:
        if run.timer is not None:
            run.timer.cancel()
            run.timer = None

    async def _expire(self, key: tuple[int, int], run: _Run) -> None:
        timeout = run.flow.conv.timeout
        assert timeout is not None
        await asyncio.sleep(timeout)
        if self.runs.get(key) is not run:
            return
        timer = run.timer
        assert timer is not None
        run.timer = None  # this task is the timer: don't cancel it
        del self.runs[key]
        self._expiring.add(timer)
        try:
            await self._exit(
                run.flow, run.update, run.context, run.state, ExitReason.TIMEOUT
            )
        finally:
            self._expiring.discard(timer)

    def _forget(self, key: tuple[int, int]) -> None:
        run = self.runs.pop(key, None)
        if run is not None and run.timer is not None:
            run.timer.cancel()

    def close(self) -> None:
        """Cancel every pending timeout and running timeout exit hook (bot
        shutdown); runs are in-memory only."""
        for run in self.runs.values():
            if run.timer is not None:
                run.timer.cancel()
        self.runs.clear()
        for timer in self._expiring:
            timer.cancel()
