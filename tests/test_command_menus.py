"""Command menus as a remote write: merge, delete, skip, seed, concurrency."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeBot
from telegram import BotCommandScopeChat, BotCommandScopeDefault

from vitrine import Bot, Conversation
from vitrine.commands import CommandMenus
from vitrine.exceptions import ConfigurationError
from vitrine.routing import Registration


def reg(command: str, scope: str = "default", description: str = "") -> Registration:
    async def fn(update): ...

    return Registration(
        kind="command",
        fn=fn,
        name=command,
        command=command,
        description=description or command,
        scope=scope,
    )


REGS = [reg("start"), reg("ban", scope="admin"), reg("vip_offer", scope="vip")]


def scopes(**chat_ids: list[int]) -> Any:
    """A scope resolver answering the same thing every time.

    ``CommandMenus.sync`` reads the scopes itself rather than taking a resolved
    mapping, so a test that does not care about *when* the read happens says so
    with this.
    """

    async def resolve() -> dict[str, list[int]]:
        return dict(chat_ids)

    return resolve


def chat_writes(fake_bot: FakeBot) -> dict[int, list[str]]:
    """chat_id -> commands, for every chat-scoped set_my_commands call."""
    writes: dict[int, list[str]] = {}
    for call in fake_bot.calls_to("set_my_commands"):
        scope = call.get("scope")
        if isinstance(scope, BotCommandScopeChat):
            writes[scope.chat_id] = [c.command for c in call["args"][0]]
    return writes


def deleted_chats(fake_bot: FakeBot) -> list[int]:
    return [call["scope"].chat_id for call in fake_bot.calls_to("delete_my_commands")]


def default_writes(fake_bot: FakeBot) -> list[list[str]]:
    return [
        [c.command for c in call["args"][0]]
        for call in fake_bot.calls_to("set_my_commands")
        if isinstance(call.get("scope"), BotCommandScopeDefault)
    ]


# ------------------------------------------------------------------ full sync


async def test_one_menu_per_chat_holds_every_scope_that_names_it(fake_bot):
    """Telegram stores a single chat-scoped menu per chat: publishing
    scope-by-scope would leave whichever scope wrote last."""
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2], vip=[2]))

    assert default_writes(fake_bot) == [["start"]]
    assert chat_writes(fake_bot) == {
        1: ["start", "ban"],
        2: ["start", "ban", "vip_offer"],
    }


async def test_a_chat_that_leaves_a_scope_drops_back_to_the_default_menu(fake_bot):
    """A demoted admin must not keep the menu they were last given."""
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert deleted_chats(fake_bot) == [2]
    assert chat_writes(fake_bot) == {}  # chat 1 unchanged: skipped


async def test_an_unchanged_resync_costs_only_the_default_write(fake_bot):
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2, 3]))
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2, 3]))

    assert chat_writes(fake_bot) == {}
    assert deleted_chats(fake_bot) == []
    assert default_writes(fake_bot) == [["start"]]  # not tracked: republished


async def test_a_chat_a_scope_reports_twice_gets_its_commands_once(fake_bot):
    """A scope's chat list may hold the same id more than once -- a resolver
    backed by a SQL join does this routinely. The menu is a function of the
    set of scopes naming the chat, not of the multiplicity."""
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 1, 1], vip=[1]))

    assert chat_writes(fake_bot) == {1: ["start", "ban", "vip_offer"]}


async def test_multiplicity_does_not_defeat_the_skip_check(fake_bot):
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1]))
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1, 1]))

    assert chat_writes(fake_bot) == {}  # the same menu, whatever the count


async def test_a_promotion_costs_one_chat_write_on_the_next_full_sync(fake_bot):
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2, 3]))

    assert chat_writes(fake_bot) == {3: ["start", "ban"]}


# ------------------------------------------------------------------ targeted sync


async def test_a_targeted_sync_touches_only_the_listed_chats(fake_bot):
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    fake_bot.calls.clear()

    # chat 2 was demoted and chat 3 promoted, but only 3 is listed: 2 keeps
    # its (now wrong) menu until a sync names it -- nothing else written,
    # nothing else cleared, not even the default menu.
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 3]), chats=[3])

    assert chat_writes(fake_bot) == {3: ["start", "ban"]}
    assert deleted_chats(fake_bot) == []
    assert default_writes(fake_bot) == []


async def test_a_targeted_sync_clears_a_listed_chat_that_left_its_scopes(fake_bot):
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]), chats=[2])

    assert deleted_chats(fake_bot) == [2]
    assert chat_writes(fake_bot) == {}


async def test_a_targeted_sync_clears_even_chats_it_never_recorded(fake_bot):
    """Records can't distinguish "never written" from "written by a previous
    process"; clearing a chat with no menu is a no-op, so delete anyway."""
    menus = CommandMenus()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]), chats=[99])

    assert deleted_chats(fake_bot) == [99]


async def test_a_skipped_write_still_skips_after_a_targeted_sync(fake_bot):
    """Targeted syncs keep the same records full syncs read."""
    menus = CommandMenus()
    await menus.sync(fake_bot, REGS, scopes(admin=[1]), chats=[1])
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert chat_writes(fake_bot) == {}


# ------------------------------------------------------------------ the seed


async def test_the_seed_clears_menus_a_previous_process_published(fake_bot):
    """A process has no memory of what a previous run wrote: an admin demoted
    while the bot was down would keep their menu forever."""
    menus = CommandMenus(known_chats=[1, 10, 11])
    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    # 1 is still scoped (menu written); 10 and 11 only *may* carry a stale
    # menu -- clearing a chat with no menu is a no-op, so a generous list is safe
    assert sorted(deleted_chats(fake_bot)) == [10, 11]
    assert chat_writes(fake_bot) == {1: ["start", "ban"]}


async def test_the_seed_is_consumed_by_the_first_sync_that_completes(fake_bot):
    menus = CommandMenus(known_chats=[10])
    await menus.sync(fake_bot, REGS, scopes(admin=[1]))
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    # re-reading the seed would re-delete every historical chat every sync
    assert deleted_chats(fake_bot) == []


async def test_a_failed_sync_does_not_eat_the_seed(fake_bot):
    """The first sync happens at startup, exactly when a database is most
    likely to be down; eating the seed there orphans every historical chat."""
    menus = CommandMenus(known_chats=[10])
    fake_bot.fail_once("set_my_commands", RuntimeError("telegram down"))

    with pytest.raises(RuntimeError):
        await menus.sync(fake_bot, REGS, scopes(admin=[1]))
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert deleted_chats(fake_bot) == [10]


async def test_a_callable_seed_is_read_once_and_retried_until_it_resolves(fake_bot):
    attempts = []

    async def known_chats():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("database still starting")
        return [10]

    menus = CommandMenus(known_chats=known_chats)
    with pytest.raises(RuntimeError):
        await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))
    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert deleted_chats(fake_bot) == [10]  # cleared once, then forgotten
    assert len(attempts) == 2  # read once per process (once it resolved)


async def test_a_targeted_sync_does_not_consume_the_seed(fake_bot):
    menus = CommandMenus(known_chats=[10])
    await menus.sync(fake_bot, REGS, scopes(admin=[1]), chats=[1])
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert deleted_chats(fake_bot) == [10]


# ------------------------------------------------------- per-chat failures


class FlakyBot(FakeBot):
    """Fails chat-scoped calls for the named chats; everything else succeeds."""

    def __init__(self, fail_chats: set[int]) -> None:
        super().__init__()
        self.fail_chats = fail_chats

    def _maybe_fail(self, method: str, args: tuple, kwargs: dict) -> bool:
        scope = kwargs.get("scope")
        if getattr(scope, "chat_id", None) in self.fail_chats:
            self.calls.append((method, {"args": args, **kwargs}))
            return True
        return False

    async def set_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        if self._maybe_fail("set_my_commands", args, kwargs):
            raise RuntimeError("boom")
        return await super().set_my_commands(*args, **kwargs)

    async def delete_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        if self._maybe_fail("delete_my_commands", args, kwargs):
            raise RuntimeError("boom")
        return await super().delete_my_commands(*args, **kwargs)


async def test_a_failed_chat_write_is_logged_and_retried_next_sync(caplog):
    flaky = FlakyBot(fail_chats={2})
    menus = CommandMenus()
    with caplog.at_level("WARNING", logger="vitrine.commands"):
        await menus.sync(flaky, REGS, scopes(admin=[1, 2]))  # startup survives

    assert chat_writes(flaky)[1] == ["start", "ban"]  # the good chat still wrote
    assert any("2" in record.getMessage() for record in caplog.records)

    flaky.fail_chats.clear()
    flaky.calls.clear()
    await menus.sync(flaky, REGS, scopes(admin=[1, 2]))

    # bookkeeping was left untouched: 2 is retried, 1 is skipped
    assert chat_writes(flaky) == {2: ["start", "ban"]}


async def test_a_failed_delete_is_retried_next_sync():
    flaky = FlakyBot(fail_chats=set())
    menus = CommandMenus()
    await menus.sync(flaky, REGS, scopes(admin=[1, 2]))

    flaky.fail_chats.add(2)
    flaky.calls.clear()
    await menus.sync(flaky, REGS, scopes(admin=[1]))
    assert deleted_chats(flaky) == [2]  # attempted, failed

    flaky.fail_chats.clear()
    flaky.calls.clear()
    await menus.sync(flaky, REGS, scopes(admin=[1]))
    assert deleted_chats(flaky) == [2]  # retried, now gone

    flaky.calls.clear()
    await menus.sync(flaky, REGS, scopes(admin=[1]))
    assert deleted_chats(flaky) == []  # ...and forgotten


# ------------------------------------------------------------- concurrency


class YieldingBot(FakeBot):
    """Suspends inside every API call, so overlapping syncs *could* interleave."""

    async def set_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(0)
        return await super().set_my_commands(*args, **kwargs)

    async def delete_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        await asyncio.sleep(0)
        return await super().delete_my_commands(*args, **kwargs)


async def test_concurrent_syncs_do_not_lose_each_others_bookkeeping():
    """Two overlapping syncs that both skip-check against stale records would
    strand a chat with the wrong menu; serialized, the second one skips."""
    bot = YieldingBot()
    menus = CommandMenus()

    await asyncio.gather(
        menus.sync(bot, REGS, scopes(admin=[1, 2, 3])),
        menus.sync(bot, REGS, scopes(admin=[1, 2, 3])),
    )

    chat_calls = [
        call
        for call in bot.calls_to("set_my_commands")
        if isinstance(call.get("scope"), BotCommandScopeChat)
    ]
    assert len(chat_calls) == 3  # one write per chat, not one per chat per sync


async def test_a_sync_reads_the_scopes_only_once_the_previous_one_has_written():
    """Serializing the writes alone is not enough.

    Read outside the lock, two overlapping syncs each snapshot the world
    first, and whichever writes *last* wins with whatever it read *first* --
    a just-demoted admin keeps their menu, and the records call it correct,
    so every later sync skips the chat. The read is part of the
    reconciliation, so the lock has to cover it too.
    """
    bot = YieldingBot()
    menus = CommandMenus()
    writes_seen_at_read: list[int] = []

    async def resolve() -> dict[str, list[int]]:
        writes_seen_at_read.append(len(bot.calls))
        await asyncio.sleep(0)  # a database round trip
        return {"admin": [1, 2]}

    await asyncio.gather(
        menus.sync(bot, REGS, resolve),
        menus.sync(bot, REGS, resolve),
    )

    # nothing written yet for the first sync; the default menu and both chats
    # for the second, which took its snapshot only after the first was done
    assert writes_seen_at_read == [0, 3]


# ------------------------------------------------- configuration errors


def make_bot(**kwargs) -> Bot:
    return Bot(token="123:TEST", **kwargs)


def test_naming_default_in_scope_chats_is_a_configuration_error():
    with pytest.raises(ConfigurationError, match="default"):
        make_bot(scope_chats={"default": [1]})


def test_a_scope_with_no_chats_is_a_configuration_error():
    """scope="admins" against scope_chats={"admin": ...} is almost always a
    typo: the command reaches /help and nobody's Telegram menu."""
    bot = make_bot(scope_chats={"admin": [1]})

    @bot.command("ban", scope="admins")
    async def ban(update): ...

    with pytest.raises(ConfigurationError, match="admins"):
        bot._wire_handlers()


def test_a_conversation_entry_scope_is_checked_too():
    bot = make_bot(scope_chats={"admin": [1]})
    conv = Conversation("t_scope_conv")

    @conv.entry(command="audit", scope="admins")
    async def audit(update): ...

    @conv.state("s")
    async def s(update): ...

    bot.conversation(conv)
    with pytest.raises(ConfigurationError, match="admins"):
        bot._wire_handlers()


def test_scope_is_just_a_help_grouping_without_scope_chats():
    bot = make_bot()

    @bot.command("ban", scope="admins")
    async def ban(update): ...

    bot._wire_handlers()  # no scope_chats configured: nothing to check against


def test_a_hidden_commands_scope_is_not_checked():
    """Hidden commands reach neither /help nor any menu, so their scope is
    inert -- rejecting it would reject a working app."""
    bot = make_bot(scope_chats={"admin": [1]})

    @bot.command("internal", scope="whatever", hidden=True)
    async def internal(update): ...

    bot._wire_handlers()


@pytest.mark.parametrize("name", ["myCmd", "with space", "über", "x" * 33, "-x"])
def test_telegram_rejected_command_names_fail_at_registration(name):
    """PTB lowercases before validating, so /myCmd dispatched fine while the
    same name made setMyCommands reject the *whole batch*."""
    bot = make_bot()

    with pytest.raises(ConfigurationError):

        @bot.command(name)
        async def handler(update): ...


def test_a_bad_function_name_fallback_is_caught_too():
    bot = make_bot()

    with pytest.raises(ConfigurationError, match="myCmd"):

        @bot.command()
        async def myCmd(update): ...  # noqa: N802


def test_conversation_decorators_validate_command_names():
    conv = Conversation("t_names")

    with pytest.raises(ConfigurationError):
        conv.entry(command="Bad")
    with pytest.raises(ConfigurationError):
        conv.state("s", command="Bad!")
    with pytest.raises(ConfigurationError):
        conv.cancel(command="STOP")


# ------------------------------------------------- bot.sync_commands()


def wire(bot: Bot, fake_bot: FakeBot) -> None:
    """Wire the handlers and stand a fake application in for the built one."""
    bot._wire_handlers()
    bot.application = SimpleNamespace(bot=fake_bot)  # type: ignore[assignment]


async def test_sync_commands_republishes_on_demand(fake_bot):
    admins = [1]
    bot = make_bot(scope_chats={"admin": lambda: admins})

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    wire(bot, fake_bot)
    await bot.sync_commands()
    assert set(chat_writes(fake_bot)) == {1}

    admins.append(2)  # a promotion takes effect without a restart
    fake_bot.calls.clear()
    await bot.sync_commands()
    assert set(chat_writes(fake_bot)) == {2}


async def test_sync_commands_with_chats_touches_only_those(fake_bot):
    bot = make_bot(scope_chats={"admin": [1, 2]})

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    wire(bot, fake_bot)
    await bot.sync_commands(chats=[1])

    assert set(chat_writes(fake_bot)) == {1}
    assert default_writes(fake_bot) == []


async def test_sync_commands_needs_the_built_application():
    bot = make_bot()
    with pytest.raises(ConfigurationError, match="build"):
        await bot.sync_commands()


async def test_known_scope_chats_seeds_the_bot_level_sync(fake_bot):
    bot = make_bot(scope_chats={"admin": [1]}, known_scope_chats=[1, 7])

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    wire(bot, fake_bot)
    await bot.sync_commands()

    assert deleted_chats(fake_bot) == [7]
