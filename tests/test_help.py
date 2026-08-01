"""/help <command>: the detail screen, visibility, and paragraph descriptions."""

from __future__ import annotations

from conftest import make_context, make_dispatch, make_update

from vitrine import Auth, Bot, Conversation, Greedy, admin_only, throttle
from vitrine.routing import split_docstring


class U:
    def __init__(self, admin: bool) -> None:
        self.admin = admin


async def resolver(update):
    return U(admin=update.effective_user.id == 1)


def make_bot() -> Bot:
    auth = Auth(resolver, name="user", is_admin=lambda u: u.admin)
    bot = Bot(token="123:TEST", auth=auth)

    @bot.command("pay")
    @throttle(3, per=60)
    async def pay(update, amount: float, note: Greedy = Greedy("")):
        """Send credits to another user.

        Transfers are instant and cannot be reversed, so double-check the
        amount before you confirm.
        """

    @bot.command("ban", scope="admin")
    @admin_only
    async def ban(update, user_id: int):
        """Ban a user."""

    bot._wire_handlers()
    return bot


def help_reg(bot: Bot):
    return next(r for r in bot._registrations if r.command == "help")


async def ask(bot: Bot, query: str, *, user_id: int = 1) -> str:
    screen = await help_reg(bot).fn(make_update(user_id=user_id), make_context(), query)
    text, _ = screen.content()
    return text.replace("\\", "")


async def test_detail_shows_usage_docstring_args_and_governance():
    text = await ask(make_bot(), "pay")

    assert "/pay <amount> [note...]" in text  # the usage line
    assert "cannot be reversed" in text  # the whole docstring, not the summary
    assert "amount" in text and "required" in text
    assert "note" in text and "optional" in text
    assert "Rate limit: 3 per 60s" in text


async def test_detail_shows_scope_and_guards():
    text = await ask(make_bot(), "ban")

    assert "admin scope" in text
    assert "admins only" in text
    assert "/ban <user_id>" in text


async def test_an_invisible_command_answers_exactly_like_a_missing_one():
    """/help ban must not confirm to a non-admin that /ban is a thing."""
    bot = make_bot()
    for_hidden = await ask(bot, "ban", user_id=2)
    for_missing = await ask(bot, "zzz", user_id=2)

    assert "Unknown command" in for_hidden
    assert for_hidden.replace("ban", "?") == for_missing.replace("zzz", "?")


async def test_a_hidden_command_is_equally_unknown():
    bot = Bot(token="123:TEST")

    @bot.command("secret", hidden=True)
    async def secret(update): ...

    bot._wire_handlers()
    text = await ask(bot, "secret")

    assert "Unknown command" in text


async def test_accepts_what_users_actually_paste():
    bot = make_bot()
    canonical = await ask(bot, "pay")

    for query in ("/Pay@mybot", " pay ", "pay", "/PAY"):
        assert await ask(bot, query) == canonical


async def test_help_without_an_argument_still_lists(fake_bot):
    bot = make_bot()
    dispatch = make_dispatch(fake_bot)

    await dispatch.run(help_reg(bot), make_update(text="/help"), make_context(fake_bot))
    listing = fake_bot.calls_to("send_message")[0]["text"]

    assert "Available commands" in listing

    # ...and the argument travels through the ordinary arg-parsing pipeline
    await dispatch.run(help_reg(bot), make_update(text="/help pay"), make_context(fake_bot))
    detail = fake_bot.calls_to("send_message")[1]["text"]
    assert "Usage" in detail


async def test_conversation_entry_args_appear_in_help():
    bot = Bot(token="123:TEST")
    conv = Conversation("t_help_args")

    @conv.entry(command="order", args=True)
    async def start(state, update, sku: str, qty: int = 1):
        """Order a product by its SKU."""

    @conv.state("qty")
    async def qty_step(state, update): ...

    bot.conversation(conv)
    bot._wire_handlers()
    text = await ask(bot, "order")

    assert "/order <sku> [qty]" in text


async def test_an_entry_without_args_shows_no_phantom_arguments():
    """An entry's extra parameters are injected; /help must not present them
    as a usage line."""
    bot = Bot(token="123:TEST")
    bot.provide_value("catalog", object())
    conv = Conversation("t_help_noargs")

    @conv.entry(command="browse")
    async def start(state, update, catalog):
        """Browse the products."""

    @conv.state("page")
    async def page(state, update): ...

    bot.conversation(conv)
    bot._wire_handlers()
    text = await ask(bot, "browse")

    assert "Usage: `/browse`" in text  # bare: no <catalog> placeholder
    assert "<catalog>" not in text and "[catalog]" not in text


# ------------------------------------------------- paragraph descriptions


def test_a_description_is_the_first_paragraph_not_the_first_line():
    """A summary that wraps across two source lines was being cut at the wrap
    -- in /help, in the published Telegram description -- and the orphaned
    remainder then opened the detail text."""

    async def transfer(update):
        """Send credits to another user, instantly
        and without any fees.

        The detail body starts here.
        """

    summary, detail = split_docstring(transfer)
    assert summary == "Send credits to another user, instantly and without any fees."
    assert detail == "The detail body starts here."


def test_registered_descriptions_use_the_paragraph():
    bot = Bot(token="123:TEST")

    @bot.command("transfer")
    async def transfer(update):
        """Send credits,
        wrapped across lines.

        Details."""

    reg = bot.router.registrations[0]
    assert reg.description == "Send credits, wrapped across lines."


async def test_the_detail_body_excludes_the_summary_paragraph():
    bot = Bot(token="123:TEST")

    @bot.command("transfer")
    async def transfer(update):
        """Send credits,
        wrapped across lines.

        Details start here."""

    bot._wire_handlers()
    text = await ask(bot, "transfer")

    assert "Details start here." in text
    assert text.count("wrapped across lines.") == 1  # summary shown once, up top
