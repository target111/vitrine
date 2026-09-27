"""Conversations end to end: runs, transitions, exits, timeouts, exclusivity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from telegram.ext import filters

from vitrine import (
    ANY_STATE,
    CANCEL,
    END,
    Arg,
    Auth,
    Bot,
    CallbackData,
    Conversation,
    ExitReason,
    Screen,
    admin_only,
)
from vitrine.exceptions import ConfigurationError
from vitrine.testing import TestClient, sent_texts


@dataclass
class OrderState:
    item: str | None = None
    qty: int = 0
    log: list = field(default_factory=list)


class StopCB(CallbackData, prefix="t_conv_stop"):
    pass


class SplitCB(CallbackData, prefix="t_split"):
    kind: str


def make_bot(*convs: Conversation, **kwargs) -> Bot:
    bot = Bot(token="123:TEST", help_command=False, **kwargs)
    for conv in convs:
        bot.conversation(conv)
    return bot


def build_conv(*, timeout: float | None = 60):
    exits: list[tuple[OrderState, ExitReason]] = []
    conv = Conversation("order", OrderState, timeout=timeout)

    @conv.entry(command="order")
    async def start(state: OrderState):
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
    async def cancelled(state: OrderState):
        return Screen(text="cancelled")

    @conv.on_exit
    async def on_exit(state: OrderState, reason: ExitReason):
        exits.append((state, reason))

    return conv, exits


def live(conv: Conversation, chat: int = 42, user: int = 42):
    return conv.active(chat_id=chat, user_id=user)


# ------------------------------------------------------------------ the basics


async def test_full_run_transitions_and_finishes():
    conv, exits = build_conv()
    async with TestClient(make_bot(conv)) as client:
        await client.send("/order")
        assert live(conv)[0] == "item"
        await client.send("widget")
        await client.send("3")

    ((state, reason),) = exits
    assert reason is ExitReason.FINISHED
    assert state.item == "widget" and state.qty == 3
    assert client.texts() == ["what?", "how many?", "done"]
    assert live(conv) is None


async def test_state_object_created_fresh_per_run():
    conv, _ = build_conv()
    async with TestClient(make_bot(conv)) as client:
        await client.send("/order")
        await client.send("first")
        await client.send("/cancel")
        await client.send("/order")
        await client.send("second")

        assert live(conv) == ("qty", OrderState(item="second"))


async def test_a_run_lives_in_its_chat_by_default():
    conv, _ = build_conv()
    async with TestClient(make_bot(conv)) as client:
        await client.send("/order", chat_id=-100)
        assert live(conv, chat=-100) is not None
        assert live(conv, chat=42) is None


async def test_per_chat_false_follows_the_user_across_chats():
    conv = Conversation("t_follow", OrderState, per_chat=False)

    @conv.entry(command="follow")
    async def start():
        return "item"

    @conv.state("item")
    async def got(state: OrderState, update):
        state.item = update.effective_message.text
        return END

    async with TestClient(make_bot(conv)) as client:
        await client.send("/follow", chat_id=-100)
        await client.send("from elsewhere", chat_id=-200)

    assert conv.active(user_id=42) is None  # finished in the other chat


def test_per_chat_and_per_user_cannot_both_be_off():
    with pytest.raises(ConfigurationError, match="both"):
        Conversation("t_nobody", per_chat=False, per_user=False)


async def test_cancel_runs_exit_hook_with_cancelled():
    conv, exits = build_conv()
    async with TestClient(make_bot(conv)) as client:
        await client.send("/order")
        assert sent_texts(await client.send("/cancel")) == ["cancelled"]

    assert exits[0][1] is ExitReason.CANCELLED
    assert live(conv) is None


async def test_a_step_can_cancel_by_returning_cancel():
    """A Cancel *button* reports CANCELLED too, not FINISHED."""
    exits: list[ExitReason] = []
    conv = Conversation("t_cancel_btn", OrderState)

    @conv.entry(command="go")
    async def start():
        return "item", Screen(text="pick")

    @conv.state("item")
    async def item(): ...

    @conv.state(ANY_STATE, callback=StopCB)
    async def stop():
        return CANCEL, Screen(text="stopped")

    @conv.on_exit
    async def on_exit(reason):
        exits.append(reason)

    async with TestClient(make_bot(conv)) as client:
        await client.send("/go")
        await client.press(StopCB())

    assert exits == [ExitReason.CANCELLED]


# -------------------------------------------------------------------- timeouts


async def test_timeout_runs_exit_hook_with_timeout():
    conv, exits = build_conv(timeout=0.02)
    async with TestClient(make_bot(conv)) as client:
        await client.send("/order")
        await asyncio.sleep(0.1)

        assert [reason for _, reason in exits] == [ExitReason.TIMEOUT]
        assert live(conv) is None


async def test_activity_resets_the_timeout():
    conv, exits = build_conv(timeout=0.4)
    async with TestClient(make_bot(conv)) as client:
        await client.send("/order")
        await asyncio.sleep(0.25)
        await client.send("widget")  # resets the clock
        await asyncio.sleep(0.25)
        assert exits == [] and live(conv)[0] == "qty"


async def test_a_slow_step_does_not_time_out_under_itself():
    exits: list[ExitReason] = []
    conv = Conversation("t_slow", OrderState, timeout=0.05)

    @conv.entry(command="slow")
    async def start():
        return "item"

    @conv.state("item")
    async def got_item(state: OrderState, update):
        await asyncio.sleep(0.15)  # outlasts the timeout
        state.item = update.effective_message.text
        return "qty", Screen(text="how many?")

    @conv.state("qty")
    async def got_qty(): ...

    @conv.on_exit
    async def on_exit(reason: ExitReason):
        exits.append(reason)

    async with TestClient(make_bot(conv)) as client:
        await client.send("/slow")
        await client.send("widget")
        assert exits == []
        assert live(conv) == ("qty", OrderState(item="widget"))

        await asyncio.sleep(0.15)  # the clock restarted once the step finished
        assert exits == [ExitReason.TIMEOUT]


async def test_an_exit_hook_can_say_goodbye_on_timeout():
    conv = Conversation("t_bye", timeout=0.02)

    @conv.entry(command="bye")
    async def start():
        return "s"

    @conv.state("s")
    async def s(): ...

    @conv.on_exit
    async def on_exit(reason):
        if reason is ExitReason.TIMEOUT:
            return Screen(text="timed out")

    async with TestClient(make_bot(conv)) as client:
        await client.send("/bye")
        await asyncio.sleep(0.1)

    assert client.texts() == ["timed out"]


async def test_stopping_the_bot_cancels_pending_timeouts():
    conv, exits = build_conv(timeout=0.05)
    async with TestClient(make_bot(conv)) as client:
        await client.send("/order")
    await asyncio.sleep(0.1)

    assert exits == []


# ---------------------------------------------------- errors stay in the pipeline


async def test_an_unknown_state_gets_error_ux_and_the_run_stays_put():
    conv = Conversation("bad", OrderState)

    @conv.entry(command="bad")
    async def start():
        return "real"

    @conv.state("real")
    async def real():
        return "no-such-state"

    async with TestClient(make_bot(conv)) as client:
        await client.send("/bad")
        calls = await client.send("anything")

        assert "went wrong" in sent_texts(calls)[0]
        assert live(conv)[0] == "real"


async def test_a_failing_step_keeps_the_run_in_its_state():
    conv = Conversation("t_boom", OrderState)

    @conv.entry(command="boom")
    async def start():
        return "s"

    @conv.state("s")
    async def s():
        raise RuntimeError("kaboom")

    async with TestClient(make_bot(conv)) as client:
        await client.send("/boom")
        calls = await client.send("x")

        assert "kaboom" not in sent_texts(calls)[0]
        assert live(conv)[0] == "s"


async def test_a_failing_exit_hook_still_ends_the_run():
    conv = Conversation("t_hookboom")

    @conv.entry(command="hb")
    async def start():
        return "s"

    @conv.state("s")
    async def s():
        return END

    @conv.on_exit
    async def on_exit():
        raise RuntimeError("hook")

    async with TestClient(make_bot(conv)) as client:
        await client.send("/hb")
        calls = await client.send("x")

    assert "went wrong" in sent_texts(calls)[0]
    assert live(conv) is None


# ----------------------------------------------------- injection and middleware


async def test_exit_hook_gets_injected_services():
    seen = []
    conv = Conversation("inj", OrderState)

    @conv.entry(command="inj")
    async def start():
        return END

    @conv.state("noop")
    async def noop(): ...

    @conv.on_exit
    async def hook(state, reason, order_service):
        seen.append((reason, order_service))

    bot = make_bot(conv)
    bot.provide_value("order_service", {"name": "svc"})
    async with TestClient(bot) as client:
        await client.send("/inj")

    assert seen == [(ExitReason.FINISHED, {"name": "svc"})]


async def test_steps_and_exit_hooks_go_through_middleware():
    order: list[str] = []
    conv, _ = build_conv()
    bot = make_bot(conv)

    @bot.middleware
    async def mw(event, call_next):
        order.append(event.handler_name)
        return await call_next(event)

    async with TestClient(bot) as client:
        await client.send("/order")
        await client.send("w")
        await client.send("/cancel")

    assert order == ["order.start", "order.got_item", "order.cancelled", "order.on_exit"]


async def test_a_middleware_that_answers_itself_moves_no_run():
    """Its return value isn't a step result: no run starts, no state changes."""
    closed = False
    conv = Conversation("t_gate")

    @conv.entry(command="gate")
    async def start():
        return "ask", Screen(text="ask?")

    @conv.state("ask")
    async def ask(update):
        return END, Screen(text=f"got {update.effective_message.text}")

    bot = make_bot(conv)

    @bot.middleware
    async def maintenance(event, call_next):
        if closed:
            return False
        return await call_next(event)

    async with TestClient(bot) as client:
        closed = True
        await client.send("/gate")
        assert conv.active(chat_id=client.chat_id, user_id=client.user_id) is None

        closed = False
        await client.send("/gate")
        closed = True
        await client.send("first")  # swallowed: the run stays in "ask"
        closed = False
        assert sent_texts(await client.send("second")) == ["got second"]


async def test_a_step_sees_what_its_ptb_handler_collects():
    """context.matches for a Regex filter, as a plain MessageHandler would get."""
    conv = Conversation("t_regex")

    @conv.entry(command="rx")
    async def start():
        return "code"

    @conv.state("code", filters=filters.Regex(r"^(\d{4})$"))
    async def code(context):
        return END, Screen(text=f"code {context.matches[0].group(1)}")

    async with TestClient(make_bot(conv)) as client:
        await client.send("/rx")
        assert sent_texts(await client.send("1234")) == ["code 1234"]


# --------------------------------------------------------- entry discoverability


def build_menu_conv() -> Conversation:
    conv = Conversation("t_menu", OrderState)

    @conv.entry(command="order", description="Place an order", scope="admin")
    async def start():
        return "item"

    @conv.entry(callback=StopCB)
    async def start_from_button():
        return "item"

    @conv.state("item", command="skip")
    async def skipped():
        return END, Screen(text="skipped")

    @conv.state("item")
    async def got_item():
        return END

    @conv.cancel()
    async def cancelled(): ...

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
    async def start():
        """
        Place an order.

        The rest of the docstring is not the description.
        """

    assert conv.command_registrations()[0].description == "Place an order."


def test_a_blank_docstring_leaves_the_description_empty():
    conv = Conversation("t_blankdoc", OrderState)

    @conv.entry(command="order")
    async def start():
        """ """

    assert conv.command_registrations()[0].description == ""


def test_in_run_commands_are_not_discoverable():
    commands = {reg.command for reg in build_menu_conv().command_registrations()}

    assert "skip" not in commands and "cancel" not in commands


async def test_a_state_command_works_only_in_its_state():
    bot = make_bot(build_menu_conv())

    @bot.command("skip")
    async def outside():
        return Screen(text="nothing to skip")

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("/skip")) == ["nothing to skip"]
        await client.send("/order")
        assert sent_texts(await client.send("/skip")) == ["skipped"]


def test_a_step_registration_states_its_transport():
    conv = Conversation("t_kind", OrderState)

    @conv.entry(command="order")
    async def start(): ...

    @conv.state("qty", command="skip")
    async def skip(): ...

    @conv.state("qty", callback=StopCB)
    async def stop(): ...

    @conv.state("qty")
    async def qty(): ...

    regs = {step.fn.__name__: step.registration(conv.name) for step in conv._steps}
    assert regs["start"].kind == "command" and regs["skip"].kind == "command"
    assert regs["stop"].kind == "callback"
    assert regs["qty"].kind == "message"


# ------------------------------------------------------------ multi-state steps


async def test_a_wildcard_step_is_mounted_on_every_state():
    exits: list[ExitReason] = []
    conv = Conversation("t_wild", OrderState)

    @conv.entry(command="wild")
    async def start():
        return "item", Screen(text="item?")

    @conv.state("item")
    async def got_item():
        return "qty"

    @conv.state("qty")
    async def got_qty():
        return END

    @conv.state(ANY_STATE, callback=StopCB)
    async def stop():
        return END

    @conv.on_exit
    async def on_exit(reason):
        exits.append(reason)

    async with TestClient(make_bot(conv)) as client:
        await client.send("/wild")
        await client.send("thing")  # now in "qty"
        await client.press(StopCB())

    assert exits == [ExitReason.FINISHED]


async def test_several_states_can_be_named_explicitly():
    hits: list[str] = []
    conv = Conversation("t_pair", OrderState)

    @conv.entry(command="pair")
    async def start():
        return "a", Screen(text="a")

    @conv.state("a")
    async def a():
        return "b"

    @conv.state("b")
    async def b():
        return END

    @conv.state("a", "b", callback=StopCB)
    async def stop(state: OrderState):
        hits.append("stop")
        return None

    async with TestClient(make_bot(conv)) as client:
        await client.send("/pair")
        await client.press(StopCB())
        await client.send("next")
        await client.press(StopCB())

    assert hits == ["stop", "stop"]


def test_a_wildcard_without_any_state_is_a_configuration_error():
    conv = Conversation("t_empty", OrderState)

    @conv.entry(command="empty")
    async def start():
        return END

    @conv.state(ANY_STATE, callback=StopCB)
    async def stop():
        return END

    with pytest.raises(ConfigurationError, match="every state"):
        make_bot(conv).build()


def test_a_state_needs_a_name():
    conv = Conversation("t_noname", OrderState)

    with pytest.raises(ConfigurationError, match="at least one name"):

        @conv.state()
        async def nowhere(): ...


def test_a_conversation_needs_an_entry():
    conv = Conversation("t_noentry")

    @conv.state("s")
    async def s(): ...

    with pytest.raises(ConfigurationError, match="no entry points"):
        make_bot(conv).build()


# --------------------------------------------------------- build-time validation


def test_a_step_with_an_unknown_param_fails_at_build():
    conv = Conversation("t_badstep", OrderState)

    @conv.entry(command="badstep")
    async def start():
        return "item"

    @conv.state("item")
    async def got_item(state, mystery_service): ...

    with pytest.raises(ConfigurationError, match="mystery_service"):
        make_bot(conv).build()


def test_an_exit_hook_with_an_unknown_param_fails_at_build():
    conv = Conversation("t_badhook", OrderState)

    @conv.entry(command="badhook")
    async def start():
        return "item"

    @conv.state("item")
    async def got_item():
        return END

    @conv.on_exit
    async def hook(state, reason, mystery_service): ...

    with pytest.raises(ConfigurationError, match="mystery_service"):
        make_bot(conv).build()


def test_a_built_conversation_is_frozen():
    conv, _ = build_conv()
    make_bot(conv).build()

    with pytest.raises(ConfigurationError, match="already built"):

        @conv.state("item")
        async def late(): ...


# -------------------------------------------------------------------- exclusivity


def build_exclusive(name: str, *, exclusive: bool = True, timeout: float | None = 60):
    exits: list[ExitReason] = []
    conv = Conversation(name, OrderState, timeout=timeout, exclusive=exclusive)

    @conv.entry(command=name)
    async def start():
        return "item"

    @conv.state("item")
    async def got_item():
        return END

    @conv.on_exit
    async def on_exit(state, reason):
        exits.append(reason)

    return conv, exits


async def test_entering_an_exclusive_run_ends_the_other_one():
    first, first_exits = build_exclusive("t_first", timeout=0.05)
    second, _ = build_exclusive("t_second")

    async with TestClient(make_bot(first, second)) as client:
        await client.send("/t_first")
        await client.send("/t_second")

        assert first_exits == [ExitReason.CANCELLED]
        assert live(first) is None and live(second) is not None
        await asyncio.sleep(0.1)  # the ended run's timer can't fire a stale TIMEOUT
        assert first_exits == [ExitReason.CANCELLED]


async def test_a_non_exclusive_run_is_left_alone():
    quiet, quiet_exits = build_exclusive("t_quiet", exclusive=False)
    loud, _ = build_exclusive("t_loud")

    async with TestClient(make_bot(quiet, loud)) as client:
        await client.send("/t_quiet")
        await client.send("/t_loud")

        assert quiet_exits == []
        assert live(quiet) is not None


async def test_other_callers_runs_are_left_alone():
    first, first_exits = build_exclusive("t_mine")
    second, _ = build_exclusive("t_yours")

    async with TestClient(make_bot(first, second)) as client:
        await client.send("/t_mine")
        await client.send("/t_yours", user_id=7, chat_id=7)

    assert first_exits == []


# ---------------------------------------------------------------- entry args


def build_args_conv(name: str = "t_args"):
    conv = Conversation(name, OrderState)

    @conv.entry(command="order")
    async def start(state: OrderState, sku: Arg[str], qty: Arg[int] = 1):
        state.item = sku
        state.qty = qty
        return "confirm", Screen(text=f"{sku} x{qty}?")

    @conv.state("confirm")
    async def confirm():
        return END

    return conv


async def test_an_args_entry_starts_with_what_it_needs_in_hand():
    conv = build_args_conv()
    async with TestClient(make_bot(conv)) as client:
        assert sent_texts(await client.send("/order ABC 3")) == ["ABC x3?"]
        assert live(conv) == ("confirm", OrderState(item="ABC", qty=3))


async def test_bad_arguments_get_the_usage_line_and_no_run_starts():
    conv = build_args_conv()
    async with TestClient(make_bot(conv)) as client:
        (reply,) = sent_texts(await client.send("/order"))

    assert live(conv) is None
    assert "/order" in reply.replace("\\", "") and "sku" in reply


def test_arguments_on_a_non_command_step_fail_the_build():
    conv = Conversation("t_args_nocmd", OrderState)

    @conv.entry(callback=StopCB)
    async def start(sku: Arg[str]): ...

    @conv.state("s")
    async def s(): ...

    with pytest.raises(ConfigurationError, match="only commands"):
        make_bot(conv).build()


# ------------------------------------------- an entry that starts no run


async def test_a_bare_screen_entry_leaves_no_trace():
    conv = Conversation("t_noent", OrderState)

    @conv.entry(command="maybe")
    async def start():
        return Screen(text="not this time")

    @conv.state("s")
    async def s():
        return END

    async with TestClient(make_bot(conv)) as client:
        assert sent_texts(await client.send("/maybe")) == ["not this time"]

    assert live(conv) is None


async def test_a_refused_entry_does_not_end_the_callers_other_runs():
    running, running_exits = build_exclusive("t_victim")
    picky = Conversation("t_picky", OrderState, exclusive=True)

    @picky.entry(command="picky")
    async def start(sku: Arg[str]):
        return "s"

    @picky.state("s")
    async def s():
        return END

    async with TestClient(make_bot(running, picky)) as client:
        await client.send("/t_victim")
        await client.send("/picky")  # missing argument: never starts
        assert running_exits == [] and live(running) is not None

        await client.send("/picky ABC")
        assert running_exits == [ExitReason.CANCELLED]


async def test_a_guard_refused_entry_leaves_no_trace():
    async def resolver(update):
        return {"admin": False}

    conv = Conversation("t_guarded", OrderState)

    @conv.entry(command="guarded")
    @admin_only
    async def start():
        return "s"

    @conv.state("s")
    async def s():
        return END

    bot = make_bot(conv, auth=Auth(resolver, name="user", is_admin=lambda u: u["admin"]))
    async with TestClient(bot) as client:
        (refusal,) = sent_texts(await client.send("/guarded"))

    assert "not allowed" in refusal
    assert live(conv) is None


async def test_an_entry_returning_end_did_start_and_finish():
    """END counts: it ends peers and runs the exit hook, unlike a refusal."""
    running, running_exits = build_exclusive("t_bg")
    finished_exits: list[ExitReason] = []
    oneshot = Conversation("t_oneshot", OrderState, exclusive=True)

    @oneshot.entry(command="oneshot")
    async def start():
        return END, Screen(text="done already")

    @oneshot.state("s")
    async def s():
        return END

    @oneshot.on_exit
    async def on_exit(state, reason):
        finished_exits.append(reason)

    async with TestClient(make_bot(running, oneshot)) as client:
        await client.send("/t_bg")
        await client.send("/oneshot")

    assert running_exits == [ExitReason.CANCELLED]
    assert finished_exits == [ExitReason.FINISHED]
    assert live(oneshot) is None


async def test_a_peers_goodbye_follows_the_new_flows_hello():
    chatty = Conversation("t_chatty", OrderState, exclusive=True)

    @chatty.entry(command="t_chatty")
    async def chatty_start():
        return "item"

    @chatty.state("item")
    async def chatty_item():
        return END

    @chatty.on_exit
    async def chatty_exit(reason, delivery, update):
        await delivery.send(update.effective_chat.id, Screen(text="goodbye"))

    fresh = Conversation("t_fresh", OrderState, exclusive=True)

    @fresh.entry(command="t_fresh")
    async def fresh_start():
        return "item", Screen(text="hello")

    @fresh.state("item")
    async def fresh_item():
        return END

    async with TestClient(make_bot(chatty, fresh)) as client:
        await client.send("/t_chatty")
        assert sent_texts(await client.send("/t_fresh")) == ["hello", "goodbye"]


# ----------------------------------------------------- when= narrows, not swallows


def build_when_conv(hits: list[str]) -> Conversation:
    conv = Conversation("t_when", OrderState)

    @conv.entry(command="when")
    async def start():
        return "pick", Screen(text="pick one")

    @conv.state("pick", callback=SplitCB, when=lambda d: d.kind == "a")
    async def pick_a():
        hits.append("a")
        return END

    @conv.state("pick", callback=SplitCB, when=lambda d: d.kind == "b")
    async def pick_b():
        hits.append("b")
        return END

    return conv


async def test_when_narrows_instead_of_swallowing_the_press():
    hits: list[str] = []
    async with TestClient(make_bot(build_when_conv(hits))) as client:
        await client.send("/when")
        await client.press(SplitCB(kind="b"))

    assert hits == ["b"]


async def test_a_press_every_when_rejects_is_answered_by_the_catch_all():
    hits: list[str] = []
    conv = build_when_conv(hits)
    async with TestClient(make_bot(conv)) as client:
        await client.send("/when")
        calls = await client.press(SplitCB(kind="z"))

        assert hits == []
        assert [c.method for c in calls] == ["answerCallbackQuery"]
        assert live(conv)[0] == "pick"
