"""Bot assembly: wiring, validation, auto-/help, ban gate."""

from __future__ import annotations

import pytest
from conftest import FakeQuery, make_context, make_ptb_update, make_update
from telegram import BotCommandScopeChat
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)
from telegram.ext import filters as ptb_filters

from vitrine import Auth, Bot, CallbackData, Conversation, ExitReason, Router
from vitrine import commands as command_discovery
from vitrine.exceptions import ConfigurationError


class WireCB(CallbackData, prefix="t_wire"):
    page: int = 1


def make_bot(**kwargs) -> Bot:
    return Bot(token="123:TEST", **kwargs)


def command_handler(wired, command: str) -> CommandHandler:
    return next(
        handler
        for handler, _ in wired
        if isinstance(handler, CommandHandler) and command in handler.commands
    )


def test_wire_handlers_produces_ptb_handlers():
    bot = make_bot()
    sub = Router("admin")

    @bot.command("start", description="Start here")
    async def start(update): ...

    @sub.command("ban", scope="admin", hidden=False)
    async def ban(update, user_id: int): ...

    @sub.callback(WireCB)
    async def page(data): ...

    conv = Conversation("t_conv")

    @conv.entry(command="go")
    async def go(update): ...

    @conv.state("step")
    async def step(update): ...

    bot.include(sub)
    bot.conversation(conv)

    wired = bot._wire_handlers()
    kinds = [type(handler) for handler, _ in wired]

    assert kinds.count(CommandHandler) == 3  # start, ban, auto-/help
    assert CallbackQueryHandler in kinds
    assert ConversationHandler in kinds
    # auto-registered /help appears in the registrations
    assert any(reg.command == "help" for reg in bot._registrations)


def test_unresolvable_handler_param_fails_at_build_time():
    bot = make_bot()

    @bot.callback(WireCB)
    async def broken(data, mystery_service): ...

    with pytest.raises(ConfigurationError, match="mystery_service"):
        bot._wire_handlers()


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


async def test_ban_gate_blocks_banned_users():
    class U:
        def __init__(self, banned):
            self.banned = banned

    async def resolver(update):
        return U(banned=update.effective_user.id == 666)

    bot = make_bot(auth=Auth(resolver, name="user", is_banned=lambda u: u.banned))
    update = make_update(user_id=666, query=FakeQuery(data="x"))
    with pytest.raises(ApplicationHandlerStop):
        await bot._ban_gate(update, make_context())
    assert update.callback_query.answers  # told politely, once

    # a normal user passes through
    await bot._ban_gate(make_update(user_id=1), make_context())


async def test_help_screen_respects_scopes():
    class U:
        def __init__(self, admin):
            self.admin = admin

    async def resolver(update):
        return U(admin=update.effective_user.id == 1)

    bot = make_bot(auth=Auth(resolver, name="user", is_admin=lambda u: u.admin))

    @bot.command("start", description="Begin")
    async def start(update): ...

    @bot.command("ban", description="Ban a user", scope="admin")
    async def ban(update): ...

    @bot.command("secret", hidden=True)
    async def secret(update): ...

    bot._wire_handlers()
    help_reg = next(r for r in bot._registrations if r.command == "help")

    admin_screen = await help_reg.fn(make_update(user_id=1), make_context())
    text, _ = admin_screen.content()
    assert "/start" in text.replace("\\", "") and "/ban" in text.replace("\\", "")
    assert "secret" not in text

    user_screen = await help_reg.fn(make_update(user_id=2), make_context())
    text, _ = user_screen.content()
    assert "/ban" not in text.replace("\\", "")


async def test_visible_scopes_closes_provider_cleanups():
    """/help principal resolution must run generator-provider cleanups."""
    closed = []

    async def resolver(session):
        return {"id": 1}

    bot = make_bot(auth=Auth(resolver, name="user"))

    @bot.provide("session")
    async def session():
        yield "sess"
        closed.append(True)

    await bot._visible_scopes(make_update(), make_context())
    assert closed == [True]


async def test_conversation_entry_commands_reach_help(fake_bot):
    """A conversation's entry command is still a command: users must find it."""
    bot = make_bot()
    conv = Conversation("t_help_conv")

    @conv.entry(command="order", description="Place an order")
    async def order(update): ...

    @conv.state("item")
    async def item(update): ...

    bot.conversation(conv)
    wired = bot._wire_handlers()

    # it is handled by the ConversationHandler, not by a second CommandHandler
    assert [type(h) for h, _ in wired].count(CommandHandler) == 1  # only auto-/help

    help_reg = next(r for r in bot._registrations if r.command == "help")
    screen = await help_reg.fn(make_update(user_id=1), make_context())
    text, _ = screen.content()
    assert "/order" in text.replace("\\", "")
    assert "Place an order" in text


async def test_conversation_entry_commands_are_published_to_telegram(fake_bot):
    bot = make_bot()
    conv = Conversation("t_menu_conv")

    @conv.entry(command="order", description="Place an order")
    async def order(update): ...

    @conv.state("item")
    async def item(update): ...

    bot.conversation(conv)
    bot._wire_handlers()
    await command_discovery.sync_command_menus(fake_bot, bot._registrations, {})

    (published,) = fake_bot.calls_to("set_my_commands")[0]["args"]
    assert [c.command for c in published] == ["help", "order"]


async def test_a_chat_that_leaves_a_scope_loses_its_menu(fake_bot):
    """Telegram keeps every scope until it is deleted and lets a chat scope
    shadow the default one, so a demoted admin would keep the menu they were
    last given -- missing every command added since -- for good."""
    bot = make_bot(scope_chats={"admin": [7]})

    @bot.command("ban", description="Ban a user", scope="admin")
    async def ban(update): ...

    bot._wire_handlers()
    published = await command_discovery.sync_command_menus(
        fake_bot, bot._registrations, {"admin": [7]}
    )
    assert published == {7}
    assert not fake_bot.calls_to("delete_my_commands")

    published = await command_discovery.sync_command_menus(
        fake_bot, bot._registrations, {"admin": []}, published_chats=published
    )

    (cleared,) = fake_bot.calls_to("delete_my_commands")
    assert cleared["scope"].chat_id == 7
    assert published == set()


async def test_a_menu_delete_that_fails_is_retried_next_sync(fake_bot):
    bot = make_bot()

    @bot.command("start", description="Begin")
    async def start(update): ...

    bot._wire_handlers()
    fake_bot.fail_once("delete_my_commands", RuntimeError("flaky"))

    published = await command_discovery.sync_command_menus(
        fake_bot, bot._registrations, {}, published_chats={7}
    )

    assert published == {7}  # still stale, so the next sync tries again


async def test_a_scope_chat_that_fails_to_publish_is_not_then_cleared(fake_bot):
    """It is still meant to have a menu, so the next sync must retry it rather
    than treat the failure as the chat having left the scope."""
    bot = make_bot(scope_chats={"admin": [7]})

    @bot.command("ban", description="Ban a user", scope="admin")
    async def ban(update): ...

    bot._wire_handlers()
    reachable = fake_bot.set_my_commands

    async def blocked(*args, **kwargs):
        if isinstance(kwargs.get("scope"), BotCommandScopeChat):
            raise RuntimeError("bot blocked by the user")
        return await reachable(*args, **kwargs)

    fake_bot.set_my_commands = blocked
    published = await command_discovery.sync_command_menus(
        fake_bot, bot._registrations, {"admin": [7]}
    )

    assert published == set()
    assert not fake_bot.calls_to("delete_my_commands")


def test_a_command_scope_with_no_chats_fails_at_build_time():
    """Nothing publishes its menu: the command reaches /help and nowhere else."""
    bot = make_bot(scope_chats={"admin": [7]})

    @bot.command("ban", scope="admins")
    async def ban(update): ...

    with pytest.raises(ConfigurationError, match="admins"):
        bot._wire_handlers()


def test_scopes_are_help_only_when_no_scope_chats_are_configured():
    bot = make_bot()

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    bot._wire_handlers()  # grouping /help by scope needs no menu


def test_a_command_name_telegram_would_reject_fails_at_registration():
    """PTB lowercases before it validates, so /myCmd dispatches fine while the
    same name makes setMyCommands reject the whole batch, freezing every menu."""
    bot = make_bot()

    with pytest.raises(ConfigurationError, match="myCmd"):

        @bot.command("myCmd")
        async def bad(update): ...


def test_commands_ignore_edited_messages():
    """Editing an old /help into /whatever used to run /whatever."""
    bot = make_bot()

    @bot.command("start")
    async def start(update): ...

    @bot.command("audit", edits=True)
    async def audit(update): ...

    wired = bot._wire_handlers()
    start_handler = command_handler(wired, "start")

    assert start_handler.filters.check_update(make_ptb_update(text="/start"))
    assert not start_handler.filters.check_update(
        make_ptb_update(text="/start", edited=True)
    )
    assert command_handler(wired, "audit").filters.check_update(
        make_ptb_update(text="/audit", edited=True)
    )


def test_message_handlers_ignore_edited_messages():
    """An edit is a whole new update: it would re-run every matching handler."""
    bot = make_bot()

    @bot.message()
    async def echo(update): ...

    @bot.message(ptb_filters.TEXT, group=1)
    async def explicit(update): ...

    handlers = [h for h, _ in bot._wire_handlers() if isinstance(h, MessageHandler)]
    assert len(handlers) == 2
    for handler in handlers:
        assert handler.filters.check_update(make_ptb_update(text="hi"))
        assert not handler.filters.check_update(
            make_ptb_update(text="hi", edited=True)
        )


async def test_a_hidden_conversation_entry_stays_out_of_help(fake_bot):
    bot = make_bot()
    conv = Conversation("t_hidden_conv")

    @conv.entry(command="secret", hidden=True)
    async def secret(update): ...

    @conv.state("item")
    async def item(update): ...

    bot.conversation(conv)
    bot._wire_handlers()

    help_reg = next(r for r in bot._registrations if r.command == "help")
    screen = await help_reg.fn(make_update(user_id=1), make_context())
    text, _ = screen.content()
    assert "secret" not in text


async def test_exclusive_conversations_are_linked_across_routers(fake_bot):
    """Peers are found bot-wide: a sub-router's flow still ends the other one."""
    bot = make_bot()
    exits = []

    top = Conversation("t_top", exclusive=True)

    @top.entry(command="top")
    async def top_start(update):
        return "item"

    @top.state("item")
    async def top_item(update): ...

    @top.on_exit
    async def top_exit(reason):
        exits.append(reason)

    sub_router = Router("sub")
    nested = Conversation("t_nested", exclusive=True)

    @nested.entry(command="nested")
    async def nested_start(update):
        return "item"

    @nested.state("item")
    async def nested_item(update): ...

    bot.conversation(top)
    sub_router.conversation(nested)
    bot.include(sub_router)
    bot._wire_handlers()

    assert [peer.name for peer in top._peers] == ["t_nested"]

    context = make_context(fake_bot)
    update = make_update(text="/top")
    await top._handler.entry_points[0].callback(update, context)
    key = top._handler._get_key(update)
    top._handler._conversations[key] = "item"  # what PTB records for a live run

    await nested._handler.entry_points[0].callback(make_update(text="/nested"), context)

    assert exits == [ExitReason.CANCELLED]
    assert key not in top._handler._conversations
