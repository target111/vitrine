"""Bot assembly: wiring, validation, auto-/help, ban gate -- through real updates."""

from __future__ import annotations

import pytest

from vitrine import (
    Arg,
    Auth,
    Bot,
    CallbackData,
    Conversation,
    ExitReason,
    Router,
    Screen,
)
from vitrine.exceptions import ConfigurationError
from vitrine.testing import TestClient, sent_texts


class WireCB(CallbackData, prefix="t_wire"):
    page: int = 1


def make_bot(**kwargs) -> Bot:
    return Bot(token="123:TEST", **kwargs)


def plain(text: str | None) -> str:
    return (text or "").replace("\\", "")


async def test_every_kind_of_registration_is_reachable():
    bot = make_bot()
    sub = Router("admin")

    @bot.command("start", description="Start here")
    async def start():
        return Screen(text="started")

    @sub.command("ban", scope="admin")
    async def ban(user_id: Arg[int]):
        return Screen(text=f"banned {user_id}")

    @sub.callback(WireCB)
    async def page(data: WireCB):
        return Screen(text=f"page {data.page}")

    conv = Conversation("t_conv")

    @conv.entry(command="go")
    async def go():
        return "step", Screen(text="going")

    @conv.state("step")
    async def step():
        return None

    bot.include(sub)
    bot.conversation(conv)

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("/start")) == ["started"]
        assert sent_texts(await client.send("/ban 7")) == ["banned 7"]
        assert sent_texts(await client.press(WireCB(page=3))) == ["page 3"]
        assert sent_texts(await client.send("/go")) == ["going"]
        assert "Available commands" in plain((await client.send("/help"))[0].text)


def test_unresolvable_handler_param_fails_at_build_time():
    bot = make_bot()

    @bot.callback(WireCB)
    async def broken(data, mystery_service): ...

    with pytest.raises(ConfigurationError, match="mystery_service"):
        bot.build()


def test_provider_registration_forms():
    bot = make_bot()

    @bot.provide
    async def alpha():
        return 1

    @bot.provide("beta")
    def beta_factory():
        return 2

    bot.provide_value("gamma", 3)
    assert {"alpha", "beta", "gamma"} <= bot.providers.names()


async def test_a_banned_caller_pressing_a_button_is_told_once():
    async def resolver(update):
        return {"banned": update.effective_user.id == 666}

    bot = make_bot(auth=Auth(resolver, name="user", is_banned=lambda u: u["banned"]))

    @bot.callback(WireCB)
    async def page():
        return Screen(text="page")

    async with TestClient(bot, user_id=666) as client:
        await client.send("/help")  # something to press on (help isn't gated)
        calls = await client.press(WireCB())

    assert [c.method for c in calls] == ["answerCallbackQuery"]
    assert calls[0].params.get("show_alert") is True


# ---------------------------------------------------------------------- /help


def scoped_bot() -> Bot:
    async def resolver(update):
        return {"admin": update.effective_user.id == 1}

    bot = make_bot(auth=Auth(resolver, name="user", is_admin=lambda u: u["admin"]))

    @bot.command("start", description="Begin")
    async def start(): ...

    @bot.command("ban", description="Ban a user", scope="admin")
    async def ban(): ...

    @bot.command("secret", hidden=True)
    async def secret(): ...

    return bot


async def test_help_lists_what_the_caller_may_see():
    async with TestClient(scoped_bot(), user_id=1) as client:
        text = plain((await client.send("/help"))[0].text)
    assert "/start" in text and "/ban" in text and "secret" not in text

    async with TestClient(scoped_bot(), user_id=2) as client:
        text = plain((await client.send("/help"))[0].text)
    assert "/start" in text and "/ban" not in text


async def test_help_runs_the_resolvers_provider_cleanups():
    closed: list[bool] = []

    async def resolver(session):
        return {"id": 1}

    bot = make_bot(auth=Auth(resolver, name="user"))

    @bot.provide("session")
    async def session():
        yield "sess"
        closed.append(True)

    async with TestClient(bot) as client:
        await client.send("/help")

    assert closed == [True]


# ------------------------------------------------------- conversation entries


def entry_bot(*, hidden: bool = False) -> Bot:
    bot = make_bot()
    conv = Conversation("t_help_conv")

    @conv.entry(command="order", description="Place an order", hidden=hidden)
    async def order(): ...

    @conv.state("item")
    async def item(): ...

    bot.conversation(conv)
    return bot


async def test_conversation_entry_commands_reach_help():
    async with TestClient(entry_bot()) as client:
        text = plain((await client.send("/help"))[0].text)
    assert "/order" in text and "Place an order" in text


async def test_a_hidden_conversation_entry_stays_out_of_help():
    async with TestClient(entry_bot(hidden=True)) as client:
        text = plain((await client.send("/help"))[0].text)
    assert "order" not in text


async def test_conversation_entry_commands_are_published_to_telegram():
    async with TestClient(entry_bot()) as client:
        await client.bot.sync_commands()

    (published,) = client.calls("setMyCommands")
    assert [c["command"] for c in published.params["commands"]] == ["help", "order"]


# ------------------------------------------------------------ one run per caller


async def test_conversations_are_linked_across_routers():
    """Peers are found bot-wide: a sub-router's flow still ends the other one."""
    bot = make_bot()
    exits: list[ExitReason] = []

    top = Conversation("t_top")

    @top.entry(command="top")
    async def top_start():
        return "item"

    @top.state("item")
    async def top_item(): ...

    @top.on_exit
    async def top_exit(reason):
        exits.append(reason)

    sub_router = Router("sub")
    nested = Conversation("t_nested")

    @nested.entry(command="nested")
    async def nested_start():
        return "item"

    @nested.state("item")
    async def nested_item(): ...

    bot.conversation(top)
    sub_router.conversation(nested)
    bot.include(sub_router)

    async with TestClient(bot) as client:
        await client.send("/top")
        assert top.active(chat_id=42, user_id=42) is not None
        await client.send("/nested")

        assert exits == [ExitReason.CANCELLED]
        assert top.active(chat_id=42, user_id=42) is None
        assert nested.active(chat_id=42, user_id=42) is not None
