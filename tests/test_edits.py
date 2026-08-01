"""Edited messages don't re-run handlers unless a handler opts in with edits=True.

Telegram delivers an edit as a fresh update carrying the whole message, and
PTB's default filters match it -- so editing an old ``/help`` into
``/whatever`` really ran ``/whatever``, and editing any old text re-fed it to
message handlers and whatever conversation state happens to be live *now*.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from vitrine import Bot, Conversation


def command_update(text: str, *, edited: bool = False) -> Update:
    message = Message(
        message_id=1,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=1, type="private"),
        text=text,
        entities=[
            MessageEntity(
                type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0])
            )
        ],
        from_user=User(id=1, first_name="t", is_bot=False),
    )
    message.set_bot(SimpleNamespace(username="testbot"))  # type: ignore[arg-type]

    if edited:
        return Update(update_id=1, edited_message=message)
    return Update(update_id=1, message=message)


def text_update(text: str, *, edited: bool = False) -> Update:
    message = Message(
        message_id=2,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=1, type="private"),
        text=text,
        from_user=User(id=1, first_name="t", is_bot=False),
    )
    if edited:
        return Update(update_id=2, edited_message=message)
    return Update(update_id=2, message=message)


def update_of(kind: str, text: str = "hello") -> Update:
    """An update of any message-carrying kind: channel_post, business_message,
    their edited forms, or the plain (edited_)message."""
    message = Message(
        message_id=3,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=1, type="channel" if "channel" in kind else "private"),
        text=text,
        entities=[
            MessageEntity(
                type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0])
            )
        ]
        if text.startswith("/")
        else None,
        from_user=None if "channel" in kind else User(id=1, first_name="t", is_bot=False),
    )
    message.set_bot(SimpleNamespace(username="testbot"))  # type: ignore[arg-type]

    return Update(update_id=3, **{kind: message})


def wired_handler(bot: Bot, handler_type: type, index: int = 0):
    handlers = [h for h, _ in bot._wire_handlers() if type(h) is handler_type]
    return handlers[index]


def test_an_edited_command_no_longer_matches():
    bot = Bot(token="123:TEST", help_command=False)

    @bot.command("ping")
    async def ping(update): ...

    handler = wired_handler(bot, CommandHandler)
    assert handler.check_update(command_update("/ping"))
    assert not handler.check_update(command_update("/ping", edited=True))


def test_edits_true_opts_a_command_back_in():
    bot = Bot(token="123:TEST", help_command=False)

    @bot.command("ping", edits=True)
    async def ping(update): ...

    handler = wired_handler(bot, CommandHandler)
    assert handler.check_update(command_update("/ping"))
    assert handler.check_update(command_update("/ping", edited=True))


def test_an_edited_text_message_no_longer_matches():
    bot = Bot(token="123:TEST", help_command=False)

    @bot.message()
    async def echo(update): ...

    handler = wired_handler(bot, MessageHandler)
    assert handler.check_update(text_update("hello"))
    assert not handler.check_update(text_update("hello", edited=True))


def test_edits_true_opts_a_message_handler_back_in():
    bot = Bot(token="123:TEST", help_command=False)

    @bot.message(edits=True)
    async def echo(update): ...

    handler = wired_handler(bot, MessageHandler)
    assert handler.check_update(text_update("hello", edited=True))


def test_custom_filters_are_still_narrowed():
    from telegram.ext import filters as ptb_filters

    bot = Bot(token="123:TEST", help_command=False)

    @bot.message(ptb_filters.Regex("hello"))
    async def greet(update): ...

    handler = wired_handler(bot, MessageHandler)
    assert handler.check_update(text_update("hello"))
    assert not handler.check_update(text_update("hello", edited=True))


def test_conversation_entries_and_states_ignore_edits_by_default():
    conv = Conversation("t_edits")

    @conv.entry(command="go")
    async def go(state, update): ...

    @conv.state("answer")
    async def answer(state, update): ...

    from conftest import FakeBot, make_dispatch

    handler = conv.build(make_dispatch(FakeBot()), [])
    entry = handler.entry_points[0]
    step = handler.states["answer"][0]

    assert entry.check_update(command_update("/go"))
    assert not entry.check_update(command_update("/go", edited=True))
    assert step.check_update(text_update("42"))
    assert not step.check_update(text_update("42", edited=True))


def test_conversation_steps_can_opt_in():
    conv = Conversation("t_edits_in")

    @conv.entry(command="go", edits=True)
    async def go(state, update): ...

    @conv.state("answer", edits=True)
    async def answer(state, update): ...

    from conftest import FakeBot, make_dispatch

    handler = conv.build(make_dispatch(FakeBot()), [])

    assert handler.entry_points[0].check_update(command_update("/go", edited=True))
    assert handler.states["answer"][0].check_update(text_update("42", edited=True))


def test_message_handlers_keep_channel_posts_and_business_messages():
    """0.2.0 delivered these, and only the edits are the intended change: the
    exclusion subtracts edits from that reach instead of replacing it with a
    positive filter that happens to be narrower."""
    bot = Bot(token="123:TEST", help_command=False)

    @bot.message()
    async def echo(update): ...

    handler = wired_handler(bot, MessageHandler)
    assert handler.check_update(update_of("channel_post"))
    assert handler.check_update(update_of("business_message"))
    assert not handler.check_update(update_of("edited_channel_post"))
    assert not handler.check_update(update_of("edited_business_message"))


def test_command_handlers_keep_their_ptb_default_reach():
    """0.2.0 built CommandHandler with PTB's default filter -- message and
    edited_message, nothing more -- so commands never matched channel posts or
    business messages. Excluding edits must subtract from that baseline, not
    quietly widen commands to update kinds they never received."""
    bot = Bot(token="123:TEST", help_command=False)

    @bot.command("ping")
    async def ping(update): ...

    handler = wired_handler(bot, CommandHandler)
    assert handler.check_update(update_of("message", "/ping"))
    assert not handler.check_update(update_of("edited_message", "/ping"))
    assert not handler.check_update(update_of("channel_post", "/ping"))
    assert not handler.check_update(update_of("business_message", "/ping"))


def test_conversation_command_steps_match_exactly_what_router_commands_do():
    """One registration implies one handler, derived in one place -- the two
    construction sites that drifted apart must not exist anymore."""
    from conftest import FakeBot, make_dispatch

    conv = Conversation("t_edits_same")

    @conv.entry(command="go")
    async def go(state, update): ...

    @conv.state("s", command="skip")
    async def skip(state, update): ...

    handler = conv.build(make_dispatch(FakeBot()), [])

    bot = Bot(token="123:TEST", help_command=False)

    @bot.command("go")
    async def go_command(update): ...

    router_command = wired_handler(bot, CommandHandler)
    kinds = [
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
        "edited_business_message",
    ]
    for kind in kinds:
        update = update_of(kind, "/go")
        assert bool(handler.entry_points[0].check_update(update)) == bool(
            router_command.check_update(update)
        )
    for kind in kinds:
        update = update_of(kind, "/skip")
        assert bool(handler.states["s"][0].check_update(update)) == bool(kind == "message")


def test_callback_handlers_are_untouched():
    """Edits are a message-side concept; button presses have no edited form."""
    from test_callbacks import MenuCB

    bot = Bot(token="123:TEST", help_command=False)

    @bot.callback(MenuCB)
    async def on_menu(data): ...

    handler = wired_handler(bot, CallbackQueryHandler)
    assert isinstance(handler, CallbackQueryHandler)
