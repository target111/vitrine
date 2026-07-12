"""Conversation state machine: per-run state, transitions, exits, timeouts."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from telegram.ext import ConversationHandler

from vitrine.conversations import END, Conversation, ExitReason
from vitrine.exceptions import ConfigurationError
from vitrine.injection import Providers
from vitrine.screens import Screen

from conftest import FakeBot, make_context, make_dispatch, make_update


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
    assert not [
        k for k in context.chat_data if context.chat_data.get(k)
    ]  # state cleared


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
