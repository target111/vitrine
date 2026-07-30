"""Bot assembly: wiring, validation, auto-/help, ban gate."""

from __future__ import annotations

import logging

import pytest
from conftest import FakeQuery, make_context, make_ptb_update, make_update
from telegram import BotCommandScopeChat, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)
from telegram.ext import filters as ptb_filters

from vitrine import (
    Auth,
    Bot,
    CallbackData,
    Conversation,
    ExitReason,
    Greedy,
    Router,
    requires,
    throttle,
)
from vitrine import commands as command_discovery
from vitrine.exceptions import ConfigurationError


class WireCB(CallbackData, prefix="t_wire"):
    page: int = 1


def make_bot(**kwargs) -> Bot:
    return Bot(token="123:TEST", **kwargs)


async def help_text(bot, arg: str = "", *, user_id: int = 1) -> str:
    help_reg = next(r for r in bot._registrations if r.command == "help")
    screen = await help_reg.fn(make_update(user_id=user_id), make_context(), arg)
    text, _ = screen.content()

    return text.replace("\\", "")  # markdown escaping is not what these assert


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


def test_a_contradicted_annotation_warns_but_still_builds(caplog):
    """Injection is by name, so a duck-typed stand-in is legitimate: say so
    loudly at startup rather than reject an app that works."""
    bot = make_bot()
    bot.provide_value("count", "5")

    @bot.command("tally")
    async def tally(update, count: int): ...

    with caplog.at_level(logging.WARNING, logger="vitrine.build"):
        bot._wire_handlers()

    assert "annotates 'count' as int" in caplog.text
    assert "supplies str" in caplog.text


def test_strict_types_promotes_the_warning_to_a_build_failure():
    bot = make_bot(strict_types=True)
    bot.provide_value("count", "5")

    @bot.command("tally")
    async def tally(update, count: int): ...

    with pytest.raises(ConfigurationError, match="annotates 'count' as int"):
        bot._wire_handlers()


def test_framework_supplied_and_argument_annotations_are_not_type_checked(caplog):
    """`update: Update` and command arguments have their own machinery; the
    provider check must not second-guess either."""
    bot = make_bot(strict_types=True)

    @bot.command("pay")
    async def pay(update: Update, context: CallbackContext, amount: int): ...

    with caplog.at_level(logging.WARNING, logger="vitrine.build"):
        bot._wire_handlers()

    assert caplog.text == ""


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

    admin = await help_text(bot, user_id=1)
    assert "/start" in admin and "/ban" in admin
    assert "secret" not in admin

    assert "/ban" not in await help_text(bot, user_id=2)


async def test_help_for_one_command_shows_usage_and_docs():
    bot = make_bot()

    @bot.command("pay")
    @throttle(3, per=60)
    async def pay(update, amount: float, target: str = "self", note: Greedy = Greedy("")):
        """Send credits to another user.

        The transfer clears instantly and cannot be undone.
        """

    bot._wire_handlers()
    text = await help_text(bot, "pay")

    assert "/pay <amount> [target] [note...]" in text
    assert "Send credits to another user." in text
    assert "cannot be undone" in text  # the rest of the docstring, not just line 1
    assert "amount` — number, required" in text
    assert "target` — text, optional, defaults to self" in text
    assert "note...` — rest of the message, optional" in text
    assert "Limit*: 3 per 60s" in text


async def test_help_for_one_command_shows_what_it_takes_to_run():
    class U:
        def __init__(self, admin):
            self.admin = admin

    async def resolver(update):
        return U(admin=True)

    bot = make_bot(
        auth=Auth(resolver, name="user", is_admin=lambda u: u.admin),
        scope_chats={"admin": [1]},
    )

    @bot.command("grant", description="Grant a role", scope="admin")
    @requires("support")
    async def grant(user, tg_id: int): ...

    bot._wire_handlers()
    text = await help_text(bot, "grant")

    assert "Scope*: admin" in text
    assert "Requires*: support" in text


async def test_help_for_an_out_of_scope_command_says_it_does_not_exist():
    """Answering "you may not" would still confirm that /ban is a command."""

    class U:
        def __init__(self, admin):
            self.admin = admin

    async def resolver(update):
        return U(admin=update.effective_user.id == 1)

    bot = make_bot(
        auth=Auth(resolver, name="user", is_admin=lambda u: u.admin),
        scope_chats={"admin": [1]},
    )

    @bot.command("ban", description="Ban a user", scope="admin")
    async def ban(update, tg_id: int): ...

    bot._wire_handlers()

    stranger = make_bot()  # a bot with no /ban at all
    stranger._wire_handlers()

    assert "/ban <tg_id>" in await help_text(bot, "ban", user_id=1)
    assert await help_text(bot, "ban", user_id=2) == await help_text(stranger, "ban")


async def test_help_accepts_a_command_as_users_write_it():
    bot = make_bot()

    @bot.command("start", description="Begin")
    async def start(update): ...

    bot._wire_handlers()
    for written in ("start", "/start", "/Start@mybot", "  start  "):
        assert "Begin" in await help_text(bot, written)


async def test_help_for_a_hidden_command_says_it_does_not_exist():
    bot = make_bot()

    @bot.command("debug", hidden=True)
    async def debug(update): ...

    bot._wire_handlers()

    assert "No command" in await help_text(bot, "debug")


async def test_help_for_a_conversation_entry_shows_no_arguments():
    """The entry registration exists to be discovered; the step that actually
    runs is a message handler, and the flow asks for what it needs."""
    bot = make_bot()
    conv = Conversation("t_argless_conv")

    @conv.entry(command="order", description="Place an order")
    async def order(update, page=1): ...

    @conv.state("item")
    async def item(update): ...

    bot.conversation(conv)
    bot._wire_handlers()
    text = await help_text(bot, "order")

    assert "/order" in text
    assert "page" not in text and "Arguments" not in text


async def test_bare_help_still_lists_everything():
    bot = make_bot()

    @bot.command("start", description="Begin")
    async def start(update): ...

    bot._wire_handlers()
    text = await help_text(bot)

    assert "Available commands" in text and "/start" in text
    assert "/help <command>" in text  # discoverable, or nobody finds the feature


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

    text = await help_text(bot)
    assert "/order" in text
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


async def test_known_scope_chats_are_only_seeded_once(monkeypatch):
    """They exist to recover what a *restart* forgot. Re-seeding them on every
    call would re-delete each historical chat's menu forever -- one API
    round-trip per chat, per call -- and re-run the resolver each time."""
    resolved = []
    seen: list[set[int]] = []

    def history():
        resolved.append(1)
        return [100, 101, 102]

    async def fake_sync(tg_bot, regs, scope_chat_ids, *, published_chats=frozenset()):
        seen.append(set(published_chats))
        return {7}  # what this sync leaves published

    monkeypatch.setattr(command_discovery, "sync_command_menus", fake_sync)
    bot = make_bot(scope_chats={"admin": [7]}, known_scope_chats=history)

    @bot.command("ban", description="Ban a user", scope="admin")
    async def ban(update): ...

    await bot.sync_commands()
    await bot.sync_commands()

    assert seen[0] == {100, 101, 102}
    assert seen[1] == {7}  # only what we actually published, not the history
    assert resolved == [1]  # the (often DB-backed) resolver ran once


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

    assert "secret" not in await help_text(bot)


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
