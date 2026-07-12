"""Bot assembly: wiring, validation, auto-/help, ban gate."""

from __future__ import annotations

import pytest
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
)

from vitrine import Auth, Bot, CallbackData, Conversation, Router
from vitrine.exceptions import ConfigurationError

from conftest import FakeQuery, make_context, make_update


class WireCB(CallbackData, prefix="t_wire"):
    page: int = 1


def make_bot(**kwargs) -> Bot:
    return Bot(token="123:TEST", **kwargs)


def test_wire_handlers_produces_ptb_handlers():
    bot = make_bot()
    sub = Router("admin")

    @bot.command("start", description="Start here")
    async def start(update):
        ...

    @sub.command("ban", scope="admin", hidden=False)
    async def ban(update, user_id: int):
        ...

    @sub.callback(WireCB)
    async def page(data):
        ...

    conv = Conversation("t_conv")

    @conv.entry(command="go")
    async def go(update):
        ...

    @conv.state("step")
    async def step(update):
        ...

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
    async def broken(data, mystery_service):
        ...

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
    async def start(update):
        ...

    @bot.command("ban", description="Ban a user", scope="admin")
    async def ban(update):
        ...

    @bot.command("secret", hidden=True)
    async def secret(update):
        ...

    bot._wire_handlers()
    help_reg = next(r for r in bot._registrations if r.command == "help")

    admin_screen = await help_reg.fn(make_update(user_id=1), make_context())
    text, _ = admin_screen.content()
    assert "/start" in text.replace("\\", "") and "/ban" in text.replace("\\", "")
    assert "secret" not in text

    user_screen = await help_reg.fn(make_update(user_id=2), make_context())
    text, _ = user_screen.content()
    assert "/ban" not in text.replace("\\", "")
