"""/help and /help <command>: the detail screen, visibility, descriptions."""

from __future__ import annotations

from vitrine import Arg, Auth, Bot, Conversation, Greedy, admin_only, throttle
from vitrine.routing import split_docstring
from vitrine.testing import TestClient


async def resolver(update):
    return {"admin": update.effective_user.id == 1}


def make_bot() -> Bot:
    bot = Bot(
        token="123:TEST", auth=Auth(resolver, name="user", is_admin=lambda u: u["admin"])
    )

    @bot.command("pay")
    @throttle(3, per=60)
    async def pay(update, amount: Arg[float], note: Greedy = Greedy("")):
        """Send credits to another user.

        Transfers are instant and cannot be reversed, so double-check the
        amount before you confirm.
        """

    @bot.command("ban", scope="admin")
    @admin_only
    async def ban(update, user_id: Arg[int]):
        """Ban a user."""

    return bot


async def ask(bot: Bot, query: str = "", *, user_id: int = 1) -> str:
    async with TestClient(bot, user_id=user_id) as client:
        (call,) = await client.send(f"/help {query}".strip())
    return (call.text or "").replace("\\", "")


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
    for_hidden = await ask(make_bot(), "ban", user_id=2)
    for_missing = await ask(make_bot(), "zzz", user_id=2)

    assert "Unknown command" in for_hidden
    assert for_hidden.replace("ban", "?") == for_missing.replace("zzz", "?")


async def test_a_hidden_command_is_equally_unknown():
    bot = Bot(token="123:TEST")

    @bot.command("secret", hidden=True)
    async def secret(update): ...

    assert "Unknown command" in await ask(bot, "secret")


async def test_accepts_what_users_actually_paste():
    canonical = await ask(make_bot(), "pay")

    for query in ("/Pay@mybot", "pay", "/PAY"):
        assert await ask(make_bot(), query) == canonical


async def test_help_without_an_argument_lists():
    assert "Available commands" in await ask(make_bot())


async def test_conversation_entry_args_appear_in_help():
    bot = Bot(token="123:TEST")
    conv = Conversation("t_help_args")

    @conv.entry(command="order")
    async def start(state, update, sku: Arg[str], qty: Arg[int] = 1):
        """Order a product by its SKU."""

    @conv.state("qty")
    async def qty_step(state, update): ...

    bot.conversation(conv)
    assert "/order <sku> [qty]" in await ask(bot, "order")


async def test_injected_parameters_are_not_presented_as_arguments():
    bot = Bot(token="123:TEST")
    bot.provide_value("catalog", object())
    conv = Conversation("t_help_noargs")

    @conv.entry(command="browse")
    async def start(state, update, catalog):
        """Browse the products."""

    @conv.state("page")
    async def page(state, update): ...

    bot.conversation(conv)
    text = await ask(bot, "browse")

    assert "Usage: `/browse`" in text
    assert "catalog" not in text


# ------------------------------------------------- paragraph descriptions


def test_a_description_is_the_first_paragraph_not_the_first_line():
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

    text = await ask(bot, "transfer")

    assert "Details start here." in text
    assert text.count("wrapped across lines.") == 1  # summary shown once, up top
