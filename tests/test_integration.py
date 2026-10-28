"""End to end through PTB's real dispatcher: precedence, scopes, ban gate, lifecycle."""

from __future__ import annotations

import pytest

from vitrine import (
    Arg,
    Auth,
    Bot,
    CallbackData,
    Conversation,
    Router,
    Screen,
    requires_principal,
)
from vitrine.exceptions import ConfigurationError
from vitrine.testing import FakeTelegram, TestClient, sent_texts


class PickCB(CallbackData, prefix="t_int_pick"):
    n: int = 1


def make_bot(**kwargs) -> Bot:
    return Bot(token="123:TEST", help_command=False, **kwargs)


# ------------------------------------------------------------------ precedence


def feedback_bot() -> Bot:
    bot = make_bot()
    conv = Conversation("feedback")

    @conv.entry(command="feedback")
    async def start():
        return "topic", Screen(text="What about?")

    @conv.state("topic")
    async def topic(update):
        return None, Screen(text=f"conv got {update.effective_message.text}")

    @conv.cancel()
    async def cancel():
        return Screen(text="conv cancelled")

    bot.conversation(conv)

    @bot.message()
    async def echo(update):
        return Screen(text=f"echo {update.effective_message.text}")

    @bot.command("cancel")
    async def nothing_to_cancel():
        return Screen(text="nothing to cancel")

    @bot.reply_button("🏠 Home")
    async def home():
        return Screen(text="home")

    return bot


async def test_a_live_conversation_sees_its_callers_text_before_a_catch_all():
    """The bug: a plain @bot.message() used to take every answer a
    conversation state was waiting for."""
    async with TestClient(feedback_bot()) as client:
        await client.send("/feedback")
        assert sent_texts(await client.send("the docs")) == ["conv got the docs"]


async def test_without_a_live_run_the_general_handler_gets_the_text():
    async with TestClient(feedback_bot()) as client:
        assert sent_texts(await client.send("hello")) == ["echo hello"]


async def test_a_run_is_per_caller():
    async with TestClient(feedback_bot()) as client:
        await client.send("/feedback")
        other = await client.send("hi", user_id=7, chat_id=7)
        assert sent_texts(other) == ["echo hi"]


async def test_reply_keyboard_presses_outrank_a_live_conversation():
    async with TestClient(feedback_bot()) as client:
        await client.send("/feedback")
        assert sent_texts(await client.send("🏠 Home")) == ["home"]


async def test_a_conversation_fallback_outranks_a_flat_command_only_while_live():
    async with TestClient(feedback_bot()) as client:
        assert sent_texts(await client.send("/cancel")) == ["nothing to cancel"]
        await client.send("/feedback")
        assert sent_texts(await client.send("/cancel")) == ["conv cancelled"]
        assert sent_texts(await client.send("/cancel")) == ["nothing to cancel"]


async def test_a_press_no_handler_took_is_answered_by_the_catch_all():
    bot = make_bot()

    @bot.callback(PickCB, when=lambda d: d.n == 1)
    async def only_one():
        return Screen(text="one")

    @bot.command("menu")
    async def menu():
        return Screen(text="menu")

    async with TestClient(bot) as client:
        await client.send("/menu")
        calls = await client.press(PickCB(n=2))
        assert [c.method for c in calls] == ["answerCallbackQuery"]


# ------------------------------------------------------- build-time reachability


def test_a_command_registered_twice_fails_the_build():
    bot = make_bot()
    conv = Conversation("t_dup")

    @bot.command("order")
    async def order(): ...

    @conv.entry(command="order")
    async def start(): ...

    @conv.state("s")
    async def s(): ...

    bot.conversation(conv)
    with pytest.raises(ConfigurationError, match="/order is registered twice"):
        bot.build()


def test_a_callback_handler_behind_one_that_takes_everything_fails_the_build():
    bot = make_bot()

    @bot.callback(PickCB)
    async def first(): ...

    @bot.callback(PickCB, when=lambda d: d.n == 2)
    async def never(): ...

    with pytest.raises(ConfigurationError, match="never"):
        bot.build()


def test_narrowed_callback_handlers_on_one_model_are_fine():
    bot = make_bot()

    @bot.callback(PickCB, when=lambda d: d.n == 1)
    async def one(): ...

    @bot.callback(PickCB)
    async def rest(): ...

    bot.build()


def pick_conv(when=None) -> Conversation:
    conv = Conversation("t_pick")

    @conv.entry(callback=PickCB, when=when)
    async def start():
        return "s"

    @conv.state("s")
    async def s(): ...

    return conv


def test_a_callback_handler_behind_a_conversation_entry_fails_the_build():
    """Conversations come first in group 0, even when registered later, and
    an entry is tried even while the caller is mid-run."""
    bot = make_bot()

    @bot.callback(PickCB)
    async def flat(): ...

    bot.conversation(pick_conv())
    with pytest.raises(ConfigurationError, match="never run.*t_pick.start"):
        bot.build()


async def test_a_narrowed_conversation_entry_leaves_the_rest_to_a_flat_handler():
    bot = make_bot()
    bot.conversation(pick_conv(when=lambda d: d.n == 1))

    @bot.callback(PickCB)
    async def flat():
        return Screen(text="flat")

    @bot.command("menu")
    async def menu():
        return Screen(text="menu")

    async with TestClient(bot) as client:
        await client.send("/menu")
        assert sent_texts(await client.press(PickCB(n=2))) == ["flat"]


# ------------------------------------------------------------------- lifecycle


def test_registering_after_build_raises_instead_of_being_ignored():
    bot = make_bot()
    sub = Router("sub")
    bot.include(sub)
    bot.build()

    with pytest.raises(ConfigurationError, match="already built"):

        @sub.command("late")
        async def late(): ...

    with pytest.raises(ConfigurationError, match="already built"):
        bot.provide_value("late", 1)


def test_build_is_idempotent():
    bot = make_bot()
    assert bot.build() is bot.build()


def test_a_request_is_not_shared_with_polling():
    """A long poll holds its connection; sharing one transport starves the rest."""
    fake = FakeTelegram()
    app = make_bot().build(request=fake)
    assert app.bot.request is fake
    assert app.bot._request[0] is not fake  # PTB keeps (get_updates, request)


def test_rebuilding_with_other_transports_raises():
    bot = make_bot()
    fake = FakeTelegram()
    app = bot.build(request=fake)
    assert bot.build(request=fake) is app
    assert bot.build() is app

    with pytest.raises(ConfigurationError, match="fresh Bot"):
        bot.build(request=FakeTelegram())
    with pytest.raises(ConfigurationError, match="fresh Bot"):
        bot.build(get_updates_request=FakeTelegram())


async def test_a_second_test_client_on_one_bot_raises_instead_of_recording_nothing():
    bot = make_bot()
    async with TestClient(bot):
        pass

    with pytest.raises(ConfigurationError, match="fresh Bot"):
        async with TestClient(bot):
            pass


# ----------------------------------------------------- one scope per update


async def test_the_principal_and_its_session_live_for_the_whole_update():
    """The ban gate resolves the principal first; the handler must get the
    same principal *and* the same, still-open session -- not a row from a
    session the gate already closed."""
    events: list[str] = []

    class Session:
        def __init__(self) -> None:
            self.open = True

    @requires_principal
    async def whoami(user, session):
        events.append(f"handler: same={user['session'] is session} open={session.open}")
        return Screen(text="ok")

    async def resolve(update, session):
        events.append("resolve")
        return {"id": update.effective_user.id, "session": session}

    bot = make_bot(auth=Auth(resolve, name="user", is_banned=lambda u: False))

    @bot.provide("session")
    async def session():
        s = Session()
        events.append("open")
        yield s
        s.open = False
        events.append("close")

    bot.command("whoami")(whoami)

    async with TestClient(bot) as client:
        await client.send("/whoami")

    assert events == ["open", "resolve", "handler: same=True open=True", "close"]


async def test_a_failed_lookup_in_the_ban_gate_leaves_the_handler_a_fresh_session():
    """The gate's lookup aborted its session's transaction; the handler must
    not inherit that session, but look the caller up again with a new one."""
    events: list[str] = []
    sessions = iter(range(1, 10))

    async def resolve(update, session):
        if session == 1:
            raise ConnectionError("database blip")
        return {"id": update.effective_user.id, "session": session}

    bot = make_bot(auth=Auth(resolve, name="user", is_banned=lambda u: False))

    @bot.provide("session")
    async def session():
        n = next(sessions)
        events.append(f"open {n}")
        try:
            yield n
        except BaseException:
            events.append(f"rollback {n}")
            raise
        events.append(f"commit {n}")

    @bot.command("whoami")
    async def whoami(user, session):
        events.append(f"handler: session {session}, user's {user['session']}")
        return Screen(text="ok")

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("/whoami")) == ["ok"]

    assert events == [
        "open 1",
        "rollback 1",
        "open 2",
        "handler: session 2, user's 2",
        "commit 2",
    ]


async def test_each_update_gets_a_fresh_scope():
    opened: list[int] = []
    bot = make_bot()

    @bot.provide("counter")
    async def counter():
        opened.append(1)
        yield len(opened)

    @bot.command("n")
    async def n(counter):
        return Screen(text=str(counter))

    async with TestClient(bot) as client:
        await client.send("/n")
        await client.send("/n")

    assert client.texts() == ["1", "2"]


async def test_a_failing_cleanup_reaches_the_error_handlers():
    """A commit that fails after the handler returned still gets error UX."""

    class CommitFailed(Exception):
        pass

    bot = make_bot()

    @bot.provide("session")
    async def session():
        yield "s"
        raise CommitFailed

    @bot.command("order")
    async def order(session):
        return Screen(text="Order placed!")

    @bot.on_error(CommitFailed)
    async def rolled_back(update):
        return Screen(text="Your order didn't go through.")

    async with TestClient(bot) as client:
        calls = await client.send("/order")

    assert sent_texts(calls) == ["Order placed!", "Your order didn't go through."]


async def test_the_ban_gate_stops_banned_callers():
    async def resolve(update):
        return {"banned": update.effective_user.id == 666}

    bot = make_bot(auth=Auth(resolve, name="user", is_banned=lambda u: u["banned"]))

    @bot.command("ping")
    async def ping():
        return Screen(text="pong")

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("/ping", user_id=666, chat_id=666)) == []
        assert sent_texts(await client.send("/ping")) == ["pong"]


# --------------------------------------------------------------- explicit args


async def test_command_arguments_are_explicit():
    bot = make_bot()
    bot.provide_value("amount", 999)  # a provider no longer changes the command line

    @bot.command("pay")
    async def pay(amount: Arg[float], note: Arg[str] = "-"):
        return Screen(text=f"{amount} {note}")

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("/pay 2.5 lunch")) == ["2.5 lunch"]


def test_a_misspelled_service_is_a_build_error_not_an_argument():
    bot = make_bot()
    bot.provide_value("user_service", object())

    @bot.command("stats")
    async def stats(user_servce): ...

    with pytest.raises(ConfigurationError, match=r"user_servce.*Arg\[\.\.\.\]"):
        bot.build()


def test_arguments_on_a_non_command_handler_fail_the_build():
    bot = make_bot()

    @bot.callback(PickCB)
    async def pick(n: Arg[int]): ...

    with pytest.raises(ConfigurationError, match="only commands"):
        bot.build()
