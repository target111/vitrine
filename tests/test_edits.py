"""Edited messages don't re-run handlers unless a handler opts in with edits=True.

Telegram delivers an edit as a fresh update carrying the whole message: without
the exclusion, editing an old ``/help`` into ``/whatever`` would run
``/whatever``, and editing any old text would re-feed it to message handlers
and to whichever conversation step is live now.
"""

from __future__ import annotations

import datetime
from typing import Any

from telegram.ext import filters as ptb_filters

from vitrine import Bot, Conversation, Screen
from vitrine.testing import TestClient, sent_texts


def make_bot() -> Bot:
    return Bot(token="123:TEST", help_command=False)


def reply(text: str):
    async def handler():
        return Screen(text=text)

    return handler


async def test_an_edited_command_does_not_run_it():
    bot = make_bot()
    bot.command("ping")(reply("pong"))

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("/ping")) == ["pong"]
        assert sent_texts(await client.edit(1, "/ping")) == []


async def test_edits_true_opts_a_command_back_in():
    bot = make_bot()
    bot.command("ping", edits=True)(reply("pong"))

    async with TestClient(bot) as client:
        assert sent_texts(await client.edit(1, "/ping")) == ["pong"]


async def test_an_edited_text_message_is_ignored():
    bot = make_bot()
    bot.message()(reply("seen"))

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("hello")) == ["seen"]
        assert sent_texts(await client.edit(1, "hello again")) == []


async def test_edits_true_opts_a_message_handler_back_in():
    bot = make_bot()
    bot.message(edits=True)(reply("seen"))

    async with TestClient(bot) as client:
        assert sent_texts(await client.edit(1, "hello again")) == ["seen"]


async def test_custom_filters_are_still_narrowed():
    bot = make_bot()
    bot.message(ptb_filters.Regex("hello"))(reply("hi"))

    async with TestClient(bot) as client:
        assert sent_texts(await client.send("hello")) == ["hi"]
        assert sent_texts(await client.edit(1, "hello")) == []


def edits_conv(*, edits: bool) -> Conversation:
    conv = Conversation("t_edits")

    @conv.entry(command="go", edits=edits)
    async def go():
        return "answer", Screen(text="question")

    @conv.state("answer", edits=edits)
    async def answer():
        return None, Screen(text="answered")

    return conv


async def test_conversation_entries_and_steps_ignore_edits_by_default():
    bot = make_bot()
    bot.conversation(edits_conv(edits=False))

    async with TestClient(bot) as client:
        assert sent_texts(await client.edit(1, "/go")) == []
        await client.send("/go")
        assert sent_texts(await client.edit(2, "42")) == []
        assert sent_texts(await client.send("42")) == ["answered"]


async def test_conversation_steps_can_opt_in():
    bot = make_bot()
    bot.conversation(edits_conv(edits=True))

    async with TestClient(bot) as client:
        assert sent_texts(await client.edit(1, "/go")) == ["question"]
        assert sent_texts(await client.edit(1, "42")) == ["answered"]


# ------------------------------------------- the reach before edits were excluded


def message_of(kind: str, text: str) -> dict[str, Any]:
    """A message-carrying update of any kind, in Bot API form."""
    channel = "channel" in kind
    message: dict[str, Any] = {
        "message_id": 5,
        "date": int(datetime.datetime.now(datetime.UTC).timestamp()),
        "chat": {"id": -100, "type": "channel", "title": "c"}
        if channel
        else {"id": 42, "type": "private", "first_name": "u"},
        "text": text,
    }
    if not channel:
        message["from"] = {"id": 42, "is_bot": False, "first_name": "u"}
    if "business" in kind:
        message["business_connection_id"] = "b"
    if "edited" in kind:
        message["edit_date"] = message["date"]
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    return {kind: message}


async def test_message_handlers_keep_channel_posts_and_business_messages():
    bot = make_bot()
    bot.message()(reply("seen"))

    async with TestClient(bot) as client:
        for kind, expected in [
            ("channel_post", ["seen"]),
            ("business_message", ["seen"]),
            ("edited_channel_post", []),
            ("edited_business_message", []),
        ]:
            assert sent_texts(await client.feed(message_of(kind, "hello"))) == expected


async def test_commands_keep_their_ptb_default_reach():
    """Plain messages only: never channel posts or business messages."""
    bot = make_bot()
    bot.command("ping")(reply("pong"))
    bot.conversation(edits_conv(edits=False))

    async with TestClient(bot) as client:
        for text, answer in [("/ping", "pong"), ("/go", "question")]:
            assert sent_texts(await client.feed(message_of("message", text))) == [answer]
            for kind in ("edited_message", "channel_post", "business_message"):
                assert sent_texts(await client.feed(message_of(kind, text))) == []
