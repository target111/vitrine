"""Command discovery: auto-generated /help and per-chat Telegram command menus.

Commands carry metadata at registration (``description``, ``scope``,
``hidden``). From that the framework derives:

- a ``/help`` screen listing what the *caller* can see -- with
  ``scope_chats``, exactly the scopes their chat's menu carries -- with
  ``hidden=True`` handlers left out, and a ``/help <command>`` detail screen
  for a single visible command;
- Telegram command menus via :class:`CommandMenus`.

``setMyCommands`` writes to Telegram and *stays written*: Telegram resolves a
private chat's menu as chat scope -> all-private-chats -> default, and keeps
each scope until something explicitly replaces or deletes it. So publishing is
reconciliation against remote state, not a local computation:

- each chat gets **one** menu holding the default commands plus every scope
  that names it (Telegram stores a single chat-scoped menu per chat, so
  writing scope-by-scope would leave whichever scope wrote last);
- a chat no longer named by any scope gets its menu *deleted*, dropping it
  back to the default menu -- otherwise a demoted admin keeps the menu they
  were last given, and goes on missing every command added since;
- a :class:`MenuStore` remembers what each chat was last given, so a re-sync
  skips chats that are already right; Telegram's flood control is waited out.

The startup sync runs in the background, so a bot with thousands of scoped
chats starts answering immediately.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import timedelta
from functools import partial
from typing import Any, Protocol, runtime_checkable

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import RetryAfter

from .args import ArgSpec, type_label, usage_string
from .auth import guard_roles, is_admin_only, needs_principal
from .markdown import Md, code
from .ratelimit import throttle_spec
from .routing import Registration, split_docstring
from .screens import Screen

logger = logging.getLogger("vitrine.commands")

#: chats a scope may name: a static list or a (possibly async) callable
ScopeChats = Callable[[], Any] | Sequence[int]

#: reads the current scope -> chats mapping; awaited under the sync lock
ScopeResolver = Callable[[], Awaitable[Mapping[str, Sequence[int]]]]


async def resolve_chats(chats: ScopeChats) -> list[int]:
    """Evaluate one chat list, calling and awaiting as needed."""
    if callable(chats):
        value = chats()
        if inspect.isawaitable(value):
            value = await value
        return list(value)

    return list(chats)


def menu_commands(regs: Iterable[Registration]) -> list[Registration]:
    seen: set[str] = set()
    result: list[Registration] = []
    for reg in regs:
        if reg.kind != "command" or reg.hidden or not reg.command:
            continue
        if reg.command in seen:
            continue
        seen.add(reg.command)
        result.append(reg)

    return result


# ------------------------------------------------------------------------- /help


def help_screen(regs: Iterable[Registration], visible_scopes: set[str]) -> Screen:
    doc = Md().heading("Available commands")
    by_scope: dict[str, list[Registration]] = {}
    for reg in menu_commands(regs):
        if reg.scope in visible_scopes:
            by_scope.setdefault(reg.scope, []).append(reg)

    for scope in sorted(by_scope, key=lambda s: (s != "default", s)):
        if scope != "default":
            doc.blank().heading(scope.capitalize())
        for reg in by_scope[scope]:
            doc.line(code(f"/{reg.command}"), " — ", reg.description or reg.command or "")

    doc.blank().line("Send ", code("/help <command>"), " for more information.")

    return Screen(text=doc)


def normalize_command_query(text: str) -> str:
    """What users actually paste: ``/Pay@mybot``, `` pay ``, ``pay`` all resolve."""
    name = text.strip().lstrip("/")
    name = name.partition("@")[0]

    return name.strip().lower()


def resolve_command(
    regs: Iterable[Registration], visible_scopes: set[str], query: str
) -> Registration | None:
    """The visible command ``query`` names, or None.

    Hidden commands and commands outside the caller's scopes resolve to None
    exactly like commands that don't exist: ``/help ban`` must not confirm to
    a non-admin that ``/ban`` is a thing.
    """
    name = normalize_command_query(query)
    for reg in menu_commands(regs):
        if reg.command == name and reg.scope in visible_scopes:
            return reg

    return None


def unknown_command_screen(query: str) -> Screen:
    """The one answer for both "doesn't exist" and "not yours to see"."""
    name = normalize_command_query(query) or query.strip()

    return Screen(
        text=Md()
        .line("Unknown command: ", code(f"/{name}"))
        .line("Send ", code("/help"), " for the list.")
    )


def _arg_type_label(spec: ArgSpec) -> str:
    if spec.greedy:
        return "text, the rest of the line"

    return type_label(spec.annotation)


def command_detail_screen(reg: Registration, specs: Sequence[ArgSpec]) -> Screen:
    """Everything a caller can know about one command they are allowed to see."""
    assert reg.command is not None
    doc = Md().line(
        code(f"/{reg.command}"), " — " if reg.description else "", reg.description
    )
    doc.line("Usage: ", code(usage_string(reg.command, list(specs))))

    detail = split_docstring(reg.fn)[1]
    if detail:
        doc.blank()
        for line in detail.splitlines():
            doc.line(line)

    if specs:
        doc.blank().heading("Arguments")
        for spec in specs:
            parts: list[Any] = [code(spec.name), " — ", _arg_type_label(spec)]
            if spec.required:
                parts.append(", required")
            else:
                parts += [", optional (default: ", code(repr(spec.default)), ")"]
            doc.bullet(*parts)

    governs: list[str] = []
    if reg.scope != "default":
        governs.append(f"{reg.scope} scope")
    if is_admin_only(reg.fn):
        governs.append("admins only")
    roles = guard_roles(reg.fn)
    if roles:
        governs.append("requires roles: " + ", ".join(roles))
    if needs_principal(reg.fn):
        governs.append("registered users only")
    if governs:
        doc.blank().line("Access: ", "; ".join(governs))

    spec_throttle = throttle_spec(reg.fn)
    if spec_throttle is not None:
        doc.line(
            "Rate limit: ",
            f"{spec_throttle.limit} per {spec_throttle.per:g}s",
        )

    return Screen(text=doc)


# ------------------------------------------------------------------------- menus


def _bot_commands(regs: Sequence[Registration]) -> list[BotCommand]:
    return [
        BotCommand(reg.command or "", (reg.description or reg.command or "")[:256])
        for reg in regs
    ]


def fingerprint(commands: Sequence[BotCommand]) -> str:
    """A short digest of a menu: equal digests mean an identical menu."""
    payload = json.dumps([(c.command, c.description) for c in commands])
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _desired_menus(
    regs: Iterable[Registration], scope_chat_ids: Mapping[str, Sequence[int]]
) -> tuple[list[BotCommand], dict[int, list[BotCommand]]]:
    """The default menu, and the one merged menu each scoped chat should hold.

    A chat's menu depends on the *set* of scopes naming it (resolvers may list
    a chat twice), merged in sorted scope order so its fingerprint is stable.
    Each distinct scope set is built once and shared.
    """
    by_scope: dict[str, list[Registration]] = {}
    for reg in menu_commands(regs):
        by_scope.setdefault(reg.scope, []).append(reg)
    default = by_scope.get("default", [])

    chat_scopes: dict[int, frozenset[str]] = {}
    for scope, chat_ids in scope_chat_ids.items():
        for chat_id in chat_ids:
            chat_scopes[chat_id] = chat_scopes.get(chat_id, frozenset()) | {scope}

    menus: dict[frozenset[str], list[BotCommand]] = {}
    for scopes in chat_scopes.values():
        if scopes not in menus:
            menus[scopes] = _bot_commands(
                [
                    *default,
                    *(reg for scope in sorted(scopes) for reg in by_scope.get(scope, ())),
                ]
            )

    return _bot_commands(default), {
        chat_id: menus[scopes] for chat_id, scopes in chat_scopes.items()
    }


@runtime_checkable
class MenuStore(Protocol):
    """Remembers which menu each chat was last given (chat id -> fingerprint).

    The default keeps it in memory, so a restart re-sends every scoped menu
    once. A persistent store (Redis, a table) skips unchanged chats across
    restarts *and* remembers which chats hold a menu at all, so a chat that
    left every scope while the bot was down still gets cleared.
    """

    async def load(self) -> Mapping[int, str]: ...

    async def set(self, chat_id: int, fingerprint: str) -> None: ...

    async def discard(self, chat_id: int) -> None: ...


class InMemoryMenuStore:
    """Default store; lives for the process lifetime."""

    def __init__(self) -> None:
        self._data: dict[int, str] = {}

    async def load(self) -> Mapping[int, str]:
        return dict(self._data)

    async def set(self, chat_id: int, fingerprint: str) -> None:
        self._data[chat_id] = fingerprint

    async def discard(self, chat_id: int) -> None:
        self._data.pop(chat_id, None)


#: attempts per API call when Telegram answers with RetryAfter (flood control)
_MAX_ATTEMPTS = 3


async def _with_flood_control(call: Callable[[], Awaitable[Any]]) -> Any:
    """Run one Bot API call, waiting out Telegram's RetryAfter a few times."""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await call()
        except RetryAfter as exc:
            if attempt == _MAX_ATTEMPTS:
                raise
            delay = exc.retry_after
            seconds = (
                delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            )
            logger.info("flood control: retrying menu write in %.1fs", seconds)
            await asyncio.sleep(seconds)
    raise AssertionError("unreachable")


class CommandMenus:
    """Reconciles Telegram's per-chat command menus with the app's scopes.

    What was published per chat lives in a :class:`MenuStore`, loaded by the
    first sync. ``known_scope_chats`` adds chats a previous process *may* have
    written to -- needed only with a store that doesn't persist -- and is
    consumed by the first full sync that completes, so a database that is
    down at startup doesn't cost the seed. A chat whose write fails -- to
    Telegram or to the store -- stays in line for the next sync; the record
    only changes once the store has it.

    All syncing runs under one lock that covers *reading* the scopes as well
    as writing them: otherwise an older read could be written last and then
    recorded as correct.
    """

    def __init__(
        self, known_chats: ScopeChats | None = None, store: MenuStore | None = None
    ) -> None:
        self.store: MenuStore = store or InMemoryMenuStore()
        self._published: dict[int, str] | None = None  # loaded from the store
        self._seed_source: ScopeChats | None = known_chats
        self._seed: set[int] = set()
        self._lock = asyncio.Lock()

    async def sync(
        self,
        tg_bot: Any,
        regs: Iterable[Registration],
        resolve_scopes: ScopeResolver,
        chats: Sequence[int] | None = None,
    ) -> None:
        """Publish/refresh menus; with ``chats`` given, touch only those chats.

        A full sync republishes the default menu, writes every scoped chat
        whose menu would change, and deletes the menu of every chat that left
        all scopes. A targeted sync writes or deletes only the listed chats.
        """
        async with self._lock:
            if self._published is None:
                self._published = dict(await self.store.load())
            published = self._published

            default, desired = _desired_menus(regs, await resolve_scopes())
            if chats is None:
                if self._seed_source is not None:
                    self._seed |= set(await resolve_chats(self._seed_source))
                writes = set(desired)
                deletes = (set(published) | self._seed) - writes
                await _with_flood_control(
                    partial(tg_bot.set_my_commands, default, scope=BotCommandScopeDefault())
                )
                self._seed_source = None  # consumed: this sync will complete
            else:
                listed = set(chats)
                writes = set(desired) & listed
                # clearing a chat with no menu is a no-op, and the record can't
                # tell "never written" from "written by an earlier process"
                deletes = listed - set(desired)

            digests: dict[int, str] = {}  # chats share menu lists: hash each once
            for chat_id in sorted(writes):
                menu = desired[chat_id]
                digest = digests.get(id(menu))
                if digest is None:
                    digest = digests[id(menu)] = fingerprint(menu)
                if published.get(chat_id) == digest:
                    self._seed.discard(chat_id)
                    continue
                try:
                    await _with_flood_control(
                        partial(
                            tg_bot.set_my_commands,
                            desired[chat_id],
                            scope=BotCommandScopeChat(chat_id=chat_id),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one bad chat id
                    logger.warning("could not set commands for chat %s: %s", chat_id, exc)
                    continue
                try:
                    await self.store.set(chat_id, digest)
                except Exception as exc:  # noqa: BLE001
                    # it holds a menu the record doesn't name: forget the record
                    # so the next sync rewrites or clears it, whichever it needs
                    logger.warning("could not record the menu of chat %s: %s", chat_id, exc)
                    published.pop(chat_id, None)
                    self._seed.add(chat_id)
                else:
                    published[chat_id] = digest
                    self._seed.discard(chat_id)

            for chat_id in sorted(deletes):
                try:
                    await _with_flood_control(
                        partial(
                            tg_bot.delete_my_commands,
                            scope=BotCommandScopeChat(chat_id=chat_id),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not clear commands for chat %s: %s", chat_id, exc)
                    continue
                try:
                    await self.store.discard(chat_id)
                except Exception as exc:  # noqa: BLE001
                    # the record names a menu it no longer holds: forget it here,
                    # and the seed has the next full sync clear it again
                    logger.warning("could not forget the menu of chat %s: %s", chat_id, exc)
                    published.pop(chat_id, None)
                    self._seed.add(chat_id)
                else:
                    published.pop(chat_id, None)
                    self._seed.discard(chat_id)
