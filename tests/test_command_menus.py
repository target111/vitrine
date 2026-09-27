"""Command menus as a remote write: merge, delete, skip, seed, concurrency."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import FakeBot
from telegram import BotCommandScopeChat, BotCommandScopeDefault

from vitrine import Auth, Bot, Conversation, Screen
from vitrine.commands import CommandMenus
from vitrine.exceptions import ConfigurationError
from vitrine.routing import Registration
from vitrine.testing import TestClient, sent_texts


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
        bot.build()


def test_a_conversation_entry_scope_is_checked_too():
    bot = make_bot(scope_chats={"admin": [1]})
    conv = Conversation("t_scope_conv")

    @conv.entry(command="audit", scope="admins")
    async def audit(update): ...

    @conv.state("s")
    async def s(update): ...

    bot.conversation(conv)
    with pytest.raises(ConfigurationError, match="admins"):
        bot.build()


def test_scope_is_just_a_help_grouping_without_scope_chats():
    bot = make_bot()

    @bot.command("ban", scope="admins")
    async def ban(update): ...

    bot.build()  # no scope_chats configured: nothing to check against


def test_a_hidden_commands_scope_is_not_checked():
    """Hidden commands reach neither /help nor any menu, so their scope is
    inert -- rejecting it would reject a working app."""
    bot = make_bot(scope_chats={"admin": [1]})

    @bot.command("internal", scope="whatever", hidden=True)
    async def internal(update): ...

    bot.build()


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


# ------------------------------------------------------------- the store


class DictStore:
    """A 'persistent' store: survives across CommandMenus instances."""

    def __init__(self) -> None:
        self.data: dict[int, str] = {}

    async def load(self) -> dict[int, str]:
        return dict(self.data)

    async def set(self, chat_id: int, fingerprint: str) -> None:
        self.data[chat_id] = fingerprint

    async def discard(self, chat_id: int) -> None:
        self.data.pop(chat_id, None)


async def test_a_persistent_store_skips_unchanged_chats_across_restarts(fake_bot):
    store = DictStore()
    await CommandMenus(store=store).sync(fake_bot, REGS, scopes(admin=[1, 2]))
    fake_bot.calls.clear()

    restarted = CommandMenus(store=store)  # a new process, same store
    await restarted.sync(fake_bot, REGS, scopes(admin=[1, 2]))

    assert chat_writes(fake_bot) == {}


async def test_a_persistent_store_clears_chats_demoted_while_down(fake_bot):
    """No seed needed: the store remembers which chats hold a menu."""
    store = DictStore()
    await CommandMenus(store=store).sync(fake_bot, REGS, scopes(admin=[1, 2]))
    fake_bot.calls.clear()

    await CommandMenus(store=store).sync(fake_bot, REGS, scopes(admin=[1]))

    assert deleted_chats(fake_bot) == [2]
    assert set(store.data) == {1}


class FlakyStore(DictStore):
    """Refuses writes for the chats in ``down``."""

    def __init__(self) -> None:
        super().__init__()
        self.down: set[int] = set()

    async def set(self, chat_id: int, fingerprint: str) -> None:
        if chat_id in self.down:
            raise ConnectionError("store unavailable")
        await super().set(chat_id, fingerprint)

    async def discard(self, chat_id: int) -> None:
        if chat_id in self.down:
            raise ConnectionError("store unavailable")
        await super().discard(chat_id)


async def test_a_store_failure_costs_that_chat_only(fake_bot):
    store = FlakyStore()
    store.down = {2}
    menus = CommandMenus(store=store)

    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2, 3]))

    assert set(chat_writes(fake_bot)) == {1, 2, 3}
    assert set(store.data) == {1, 3}


async def test_a_menu_the_store_missed_is_retried_next_sync(fake_bot):
    store = FlakyStore()
    store.down = {2}
    menus = CommandMenus(store=store)
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    store.down = set()
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))

    assert set(chat_writes(fake_bot)) == {2}
    assert set(store.data) == {1, 2}


async def test_a_menu_the_store_missed_is_still_cleared_when_its_chat_leaves(fake_bot):
    store = FlakyStore()
    store.down = {2}
    menus = CommandMenus(store=store)
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    store.down = set()
    fake_bot.calls.clear()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert deleted_chats(fake_bot) == [2]


async def test_a_clear_the_store_missed_is_retried_next_sync(fake_bot):
    store = FlakyStore()
    menus = CommandMenus(store=store)
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    store.down = {2}
    await menus.sync(fake_bot, REGS, scopes(admin=[1]))
    assert set(store.data) == {1, 2}  # Telegram cleared it; the store didn't hear

    store.down = set()
    fake_bot.calls.clear()
    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert deleted_chats(fake_bot) == [2]
    assert set(store.data) == {1}


# ------------------------------------------------------------ flood control


async def test_flood_control_is_waited_out_not_skipped(fake_bot):
    from telegram.error import RetryAfter

    fake_bot.fail_once("set_my_commands", RetryAfter(0))  # the default menu
    fake_bot.fail_once("set_my_commands", RetryAfter(0))  # chat 1, once
    menus = CommandMenus()

    await menus.sync(fake_bot, REGS, scopes(admin=[1]))

    assert chat_writes(fake_bot) == {1: ["start", "ban"]}
    assert len(fake_bot.calls_to("set_my_commands")) == 4  # two refused, two landed


async def test_persistent_flood_control_gives_up_on_that_chat_only(fake_bot):
    from telegram.error import RetryAfter

    menus = CommandMenus()
    for _ in range(3):
        fake_bot.fail_once("set_my_commands", RetryAfter(0))

    # chat 1 is refused three times and given up on; the sync still completes
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]), chats=[1, 2])

    fake_bot.calls.clear()
    await menus.sync(fake_bot, REGS, scopes(admin=[1, 2]))
    assert chat_writes(fake_bot) == {1: ["start", "ban"]}  # retried; 2 had landed


# ------------------------------------------------- bot.sync_commands()


async def test_sync_commands_republishes_on_demand():
    admins = [1]
    bot = make_bot(scope_chats={"admin": lambda: admins})

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    async with TestClient(bot) as client:
        await bot.sync_commands()
        assert chat_ids_written(client) == {1}

        admins.append(2)  # a promotion takes effect without a restart
        client.requests.clear()
        await bot.sync_commands()
        assert chat_ids_written(client) == {2}


async def test_sync_commands_with_chats_touches_only_those():
    bot = make_bot(scope_chats={"admin": [1, 2]})

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    async with TestClient(bot) as client:
        await bot.sync_commands(chats=[1])

    assert chat_ids_written(client) == {1}
    assert len(client.calls("setMyCommands")) == 1  # not even the default menu


async def test_sync_commands_needs_the_built_application():
    bot = make_bot()
    with pytest.raises(ConfigurationError, match="build"):
        await bot.sync_commands()


async def test_known_scope_chats_seeds_the_bot_level_sync():
    bot = make_bot(scope_chats={"admin": [1]}, known_scope_chats=[1, 7])

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    async with TestClient(bot) as client:
        await bot.sync_commands()

    assert [c.params["scope"]["chat_id"] for c in client.calls("deleteMyCommands")] == [7]


async def test_the_startup_sync_does_not_hold_up_the_bot():
    """One API call per scoped chat: at startup it runs in the background."""
    release = asyncio.Event()

    async def slow_admins():
        await release.wait()  # a database that takes its time
        return [1]

    bot = make_bot(scope_chats={"admin": slow_admins})

    @bot.command("ban", scope="admin")
    async def ban(update): ...

    @bot.command("ping")
    async def ping():
        return Screen(text="pong")

    async with TestClient(bot, startup=True) as client:
        assert sent_texts(await client.send("/ping")) == ["pong"]  # answered already
        assert client.calls("setMyCommands") == []

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert chat_ids_written(client) == {1}


def chat_ids_written(client: TestClient) -> set[int]:
    return {
        call.params["scope"]["chat_id"]
        for call in client.calls("setMyCommands")
        if call.params.get("scope", {}).get("type") == "chat"
    }


# -------------------------------------------- /help agrees with the menus


def vip_bot(**kwargs) -> Bot:
    async def resolver(update):
        return {"admin": False}

    bot = make_bot(
        scope_chats={"vip": [7]},
        auth=Auth(resolver, name="user", is_admin=lambda u: u["admin"]),
        **kwargs,
    )

    @bot.command("perk", description="A VIP perk", scope="vip")
    async def perk(update): ...

    return bot


async def test_help_shows_a_scope_to_the_chats_whose_menu_has_it():
    """A non-admin VIP sees /perk in their menu -- and so in /help."""
    async with TestClient(vip_bot(), user_id=7) as client:
        listing = (await client.send("/help"))[0].text.replace("\\", "")
        detail = (await client.send("/help perk"))[0].text

    assert "/perk" in listing
    assert "Unknown command" not in detail


async def test_help_hides_a_scope_from_chats_outside_it():
    async with TestClient(vip_bot(), user_id=8) as client:
        listing = (await client.send("/help"))[0].text.replace("\\", "")

    assert "/perk" not in listing


def test_scope_chats_and_scope_member_are_mutually_exclusive():
    with pytest.raises(ConfigurationError, match="not both"):
        make_bot(scope_chats={"vip": [7]}, scope_member=lambda scope, user: True)
