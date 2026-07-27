"""Conversation state machine: per-run state, transitions, exits, timeouts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from conftest import FakeBot, FakeQuery, make_context, make_dispatch, make_update
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler

from vitrine.callbacks import CallbackData
from vitrine.conversations import ANY_STATE, END, Conversation, ExitReason
from vitrine.exceptions import ConfigurationError
from vitrine.injection import Providers
from vitrine.screens import Screen


@dataclass
class OrderState:
    item: str | None = None
    qty: int = 0
    log: list = field(default_factory=list)


def build_conv(providers: Providers | None = None):
    exits: list[tuple[OrderState, ExitReason]] = []
    conv = Conversation("order", OrderState, timeout=60)

    @conv.entry(command="order")
    async def start(state: OrderState, update):
        return "item", Screen(text="what?")

    @conv.state("item")
    async def got_item(state: OrderState, update):
        state.item = update.effective_message.text
        return "qty", Screen(text="how many?")

    @conv.state("qty")
    async def got_qty(state: OrderState, update):
        state.qty = int(update.effective_message.text)
        return END, Screen(text="done")

    @conv.cancel()
    async def cancelled(state: OrderState, update):
        return None

    @conv.on_exit
    async def on_exit(state: OrderState, reason: ExitReason):
        exits.append((state, reason))

    return conv, exits


def get_callbacks(handler):
    entry = handler.entry_points[0].callback
    states = {name: hs[0].callback for name, hs in handler.states.items()}
    fallback = handler.fallbacks[0].callback
    return entry, states, fallback


async def test_full_run_transitions_and_finishes(fake_bot: FakeBot):
    conv, exits = build_conv()
    dispatch = make_dispatch(fake_bot)
    handler = conv.build(dispatch, [])
    entry, states, _ = get_callbacks(handler)
    context = make_context(fake_bot)

    assert await entry(make_update(text="/order"), context) == "item"
    assert await states["item"](make_update(text="widget"), context) == "qty"
    result = await states["qty"](make_update(text="3"), context)
    assert result == ConversationHandler.END

    ((state, reason),) = exits
    assert reason is ExitReason.FINISHED
    assert state.item == "widget" and state.qty == 3
    # each step's screen was delivered
    texts = [c["text"] for c in fake_bot.calls_to("send_message")]
    assert texts == ["what?", "how many?", "done"]


async def test_state_object_created_fresh_per_run(fake_bot: FakeBot):
    conv, _ = build_conv()
    dispatch = make_dispatch(fake_bot)
    entry, states, _ = get_callbacks(conv.build(dispatch, []))
    context = make_context(fake_bot)

    await entry(make_update(text="/order"), context)
    await states["item"](make_update(text="first"), context)
    await entry(make_update(text="/order"), context)  # restart
    result = await states["item"](make_update(text="second"), context)
    assert result == "qty"

    key = [k for k in context.chat_data if k.startswith("__vitrine_conv:order")][0]
    assert context.chat_data[key].item == "second"


def test_per_chat_false_state_follows_user_across_chats():
    conv = Conversation("t_follow", OrderState, per_chat=False)
    user_data: dict = {}
    ctx_a = SimpleNamespace(chat_data={}, user_data=user_data)
    ctx_b = SimpleNamespace(chat_data={}, user_data=user_data)

    state = OrderState(item="widget")
    conv._set_state(make_update(user_id=7, chat_id=1), ctx_a, state)
    assert conv._get_state(make_update(user_id=7, chat_id=2), ctx_b) is state
    assert not ctx_a.chat_data  # stored on the user, not the chat


def test_per_chat_state_stays_on_the_chat():
    conv = Conversation("t_stay", OrderState)  # per_chat=True is the default
    ctx = SimpleNamespace(chat_data={}, user_data={})
    conv._set_state(make_update(user_id=7, chat_id=1), ctx, OrderState())
    assert ctx.chat_data and not ctx.user_data


async def test_cancel_runs_exit_hook_with_cancelled(fake_bot: FakeBot):
    conv, exits = build_conv()
    dispatch = make_dispatch(fake_bot)
    entry, _, fallback = get_callbacks(conv.build(dispatch, []))
    context = make_context(fake_bot)

    await entry(make_update(text="/order"), context)
    result = await fallback(make_update(text="/cancel"), context)
    assert result == ConversationHandler.END
    assert exits[0][1] is ExitReason.CANCELLED


async def test_timeout_runs_exit_hook_with_timeout(fake_bot: FakeBot):
    conv, exits = build_conv()
    dispatch = make_dispatch(fake_bot)
    handler = conv.build(dispatch, [])
    entry, states, _ = get_callbacks(handler)
    context = make_context(fake_bot)

    await entry(make_update(text="/order"), context)
    timeout_cb = handler.states[ConversationHandler.TIMEOUT][0].callback
    await timeout_cb(make_update(text="anything"), context)

    assert exits[0][1] is ExitReason.TIMEOUT
    assert not [k for k in context.chat_data if context.chat_data.get(k)]  # state cleared


async def test_unknown_state_is_a_configuration_error(fake_bot: FakeBot):
    conv = Conversation("bad", OrderState)

    @conv.entry(command="bad")
    async def start(state, update):
        return "no-such-state"

    @conv.state("real")
    async def real(state, update):
        return END

    dispatch = make_dispatch(fake_bot)
    entry = conv.build(dispatch, []).entry_points[0].callback

    with pytest.raises(ConfigurationError, match="no-such-state"):
        await entry(make_update(text="/bad"), make_context(fake_bot))


async def test_exit_hook_gets_injected_services(fake_bot: FakeBot):
    seen = []
    providers = Providers()
    providers.register_value("order_service", {"name": "svc"})

    conv = Conversation("inj", OrderState)

    @conv.entry(command="inj")
    async def start(state, update):
        return END

    @conv.state("noop")
    async def noop(state, update):
        return END

    @conv.on_exit
    async def hook(state, reason, order_service):
        seen.append((reason, order_service))

    dispatch = make_dispatch(fake_bot, providers=providers)
    entry = conv.build(dispatch, []).entry_points[0].callback

    await entry(make_update(text="/inj"), make_context(fake_bot))
    assert seen == [(ExitReason.FINISHED, {"name": "svc"})]


async def test_conversation_steps_go_through_middleware(fake_bot: FakeBot):
    order: list[str] = []

    async def mw(event, call_next):
        order.append(f"mw:{event.handler_name}")
        return await call_next(event)

    conv, _ = build_conv()
    dispatch = make_dispatch(fake_bot)
    entry, states, _ = get_callbacks(conv.build(dispatch, [mw]))
    context = make_context(fake_bot)

    await entry(make_update(text="/order"), context)
    await states["item"](make_update(text="w"), context)

    assert order == ["mw:order.start", "mw:order.got_item"]


# --------------------------------------------------------- entry discoverability


class StopCB(CallbackData, prefix="t_conv_stop"):
    pass


def build_menu_conv() -> Conversation:
    conv = Conversation("t_menu", OrderState)

    @conv.entry(command="order", description="Place an order", scope="admin")
    async def start(state, update):
        return "item"

    @conv.entry(callback=StopCB)
    async def start_from_button(state, update):
        return "item"

    @conv.state("item", command="skip")
    async def skipped(state, update):
        return END

    @conv.state("item")
    async def got_item(state, update):
        return END

    @conv.cancel()
    async def cancelled(state, update):
        return None

    return conv


def test_entry_commands_are_discoverable():
    regs = build_menu_conv().command_registrations()

    assert len(regs) == 1  # the button entry has no command
    assert regs[0].command == "order"
    assert regs[0].description == "Place an order"
    assert regs[0].scope == "admin"


def test_an_entry_description_falls_back_to_the_docstring():
    conv = Conversation("t_doc", OrderState)

    @conv.entry(command="order")
    async def start(state, update):
        """
        Place an order.

        The rest of the docstring is not the description.
        """

    @conv.state("item")
    async def got_item(state, update): ...

    assert conv.command_registrations()[0].description == "Place an order."


def test_a_blank_docstring_leaves_the_description_empty():
    """A docstring of only whitespace has no summary line -- and must not raise."""
    conv = Conversation("t_blankdoc", OrderState)

    @conv.entry(command="order")
    async def start(state, update):
        """
        """

    @conv.state("item")
    async def got_item(state, update): ...

    assert conv.command_registrations()[0].description == ""


def test_in_run_commands_are_not_discoverable():
    """/skip and /cancel do nothing outside a live run, so they stay unlisted."""
    commands = {reg.command for reg in build_menu_conv().command_registrations()}

    assert "skip" not in commands and "cancel" not in commands


async def test_a_state_command_is_a_step_of_that_state(fake_bot: FakeBot):
    handler = build_menu_conv().build(make_dispatch(fake_bot), [])

    kinds = [type(h) for h in handler.states["item"]]
    assert CommandHandler in kinds  # /skip
    assert MessageHandler in kinds  # the typed answer

    skip = handler.states["item"][0].callback
    assert await skip(make_update(text="/skip"), make_context(fake_bot)) == END


# ------------------------------------------------------------ multi-state steps


def build_wildcard_conv(fake_bot: FakeBot):
    exits: list[ExitReason] = []
    conv = Conversation("t_wild", OrderState)

    @conv.entry(command="wild")
    async def start(state, update):
        return "item"

    @conv.state("item")
    async def got_item(state, update):
        return "qty"

    @conv.state("qty")
    async def got_qty(state, update):
        return END

    @conv.state(ANY_STATE, callback=StopCB)
    async def stop(state, update):
        return END

    @conv.on_exit
    async def on_exit(state, reason):
        exits.append(reason)

    return conv, exits


async def test_a_wildcard_step_is_mounted_on_every_state(fake_bot: FakeBot):
    conv, exits = build_wildcard_conv(fake_bot)
    handler = conv.build(make_dispatch(fake_bot), [])

    assert len(handler.states["item"]) == 2  # the answer plus the stop button
    assert len(handler.states["qty"]) == 2

    context = make_context(fake_bot)
    stop_from_qty = handler.states["qty"][1].callback
    await handler.entry_points[0].callback(make_update(text="/wild"), context)
    result = await stop_from_qty(
        make_update(query=FakeQuery(data=StopCB().pack())), context
    )

    assert result == END
    assert exits == [ExitReason.FINISHED]


async def test_several_states_can_be_named_explicitly(fake_bot: FakeBot):
    conv = Conversation("t_pair", OrderState)

    @conv.entry(command="pair")
    async def start(state, update):
        return "a"

    @conv.state("a")
    async def a(state, update):
        return "b"

    @conv.state("b")
    async def b(state, update):
        return END

    @conv.state("a", "b", callback=StopCB)
    async def stop(state, update):
        return END

    handler = conv.build(make_dispatch(fake_bot), [])
    assert len(handler.states["a"]) == 2 and len(handler.states["b"]) == 2


def test_a_wildcard_without_any_state_is_a_configuration_error(fake_bot: FakeBot):
    conv = Conversation("t_empty", OrderState)

    @conv.entry(command="empty")
    async def start(state, update):
        return END

    @conv.state(ANY_STATE, callback=StopCB)
    async def stop(state, update):
        return END

    with pytest.raises(ConfigurationError, match="every state"):
        conv.build(make_dispatch(fake_bot), [])


def test_a_state_needs_a_name():
    conv = Conversation("t_noname", OrderState)

    with pytest.raises(ConfigurationError, match="at least one name"):

        @conv.state()
        async def nowhere(state, update): ...


# --------------------------------------------------------- build-time validation


def test_a_step_with_an_unknown_param_fails_at_build(fake_bot: FakeBot):
    conv = Conversation("t_badstep", OrderState)

    @conv.entry(command="badstep")
    async def start(state, update):
        return "item"

    @conv.state("item")
    async def got_item(state, update, mystery_service): ...

    with pytest.raises(ConfigurationError, match="mystery_service"):
        conv.build(make_dispatch(fake_bot), [])


def test_an_exit_hook_with_an_unknown_param_fails_at_build(fake_bot: FakeBot):
    conv = Conversation("t_badhook", OrderState)

    @conv.entry(command="badhook")
    async def start(state, update):
        return "item"

    @conv.state("item")
    async def got_item(state, update):
        return END

    @conv.on_exit
    async def hook(state, reason, mystery_service): ...

    with pytest.raises(ConfigurationError, match="mystery_service"):
        conv.build(make_dispatch(fake_bot), [])


# -------------------------------------------------------------------- exclusivity


class FakeJob:
    def __init__(self) -> None:
        self.removed = False

    def schedule_removal(self) -> None:
        self.removed = True


def build_exclusive(name: str, *, exclusive: bool = True):
    exits: list[ExitReason] = []
    conv = Conversation(name, OrderState, timeout=60, exclusive=exclusive)

    @conv.entry(command=name)
    async def start(state, update):
        return "item"

    @conv.state("item")
    async def got_item(state, update):
        return END

    @conv.on_exit
    async def on_exit(state, reason):
        exits.append(reason)

    return conv, exits


def start_ptb_run(handler, update, state: str = "item") -> tuple:
    """What PTB records after an entry point returns a state."""
    key = handler._get_key(update)
    handler._conversations[key] = state
    return key


async def test_entering_an_exclusive_run_ends_the_other_one(fake_bot: FakeBot):
    dispatch = make_dispatch(fake_bot)
    first, first_exits = build_exclusive("t_first")
    second, _ = build_exclusive("t_second")
    handler_a = first.build(dispatch, [])
    handler_b = second.build(dispatch, [])
    first.link_peers([first, second])
    second.link_peers([first, second])

    context = make_context(fake_bot)
    update = make_update(text="/t_first")
    await handler_a.entry_points[0].callback(update, context)
    key = start_ptb_run(handler_a, update)
    job = FakeJob()
    handler_a.timeout_jobs[key] = job

    await handler_b.entry_points[0].callback(make_update(text="/t_second"), context)

    assert first_exits == [ExitReason.CANCELLED]
    assert key not in handler_a._conversations  # PTB no longer routes to it
    assert job.removed  # ...and its timeout can't fire a stale notice
    assert first._get_state(update, context) is None


async def test_a_non_exclusive_run_is_left_alone(fake_bot: FakeBot):
    dispatch = make_dispatch(fake_bot)
    quiet, quiet_exits = build_exclusive("t_quiet", exclusive=False)
    loud, _ = build_exclusive("t_loud")
    handler_a = quiet.build(dispatch, [])
    handler_b = loud.build(dispatch, [])
    loud.link_peers([loud])  # only exclusive conversations are peers

    context = make_context(fake_bot)
    update = make_update(text="/t_quiet")
    await handler_a.entry_points[0].callback(update, context)
    key = start_ptb_run(handler_a, update)

    await handler_b.entry_points[0].callback(make_update(text="/t_loud"), context)

    assert quiet_exits == []
    assert key in handler_a._conversations


def test_exclusive_needs_the_ptb_internals_it_drives(fake_bot: FakeBot, monkeypatch):
    """If PTB moves what end_run reaches for, the build says so, not an update."""
    conv, _ = build_exclusive("t_guard")
    monkeypatch.delattr(ConversationHandler, "_get_key")

    with pytest.raises(ConfigurationError, match="_get_key"):
        conv.build(make_dispatch(fake_bot), [])


def test_a_non_exclusive_conversation_does_not_need_them(fake_bot: FakeBot, monkeypatch):
    """Only exclusivity touches PTB internals, so nothing else builds on them."""
    conv, _ = build_exclusive("t_noguard", exclusive=False)
    monkeypatch.delattr(ConversationHandler, "_get_key")

    assert conv.build(make_dispatch(fake_bot), []) is not None


async def test_ending_a_run_that_was_never_started_is_a_no_op(fake_bot: FakeBot):
    dispatch = make_dispatch(fake_bot)
    conv, exits = build_exclusive("t_idle")
    conv.build(dispatch, [])

    ended = await conv.end_run(dispatch, make_update(text="x"), make_context(fake_bot))

    assert ended is False
    assert exits == []
