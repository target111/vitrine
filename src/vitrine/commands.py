"""Command discovery: auto-generated /help and per-chat Telegram command menus.

Commands carry metadata at registration (``description``, ``scope``,
``hidden``). From that the framework derives:

- a ``/help`` screen listing what the *caller* can see (admins see admin
  commands), with ``hidden=True`` handlers (entry-only, internal) left out,
  and a ``/help <command>`` detail screen for a single visible command;
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
- bookkeeping of what was last written lets a re-sync skip chats that are
  already right, and a recovery seed covers chats a previous process wrote to.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from .args import ArgSpec, type_label, usage_string
from .auth import guard_roles, is_admin_only, needs_principal
from .markdown import Md, code
from .ratelimit import throttle_spec
from .routing import Registration, split_docstring
from .screens import Screen

logger = logging.getLogger("vitrine.commands")

#: chats a scope may name: a static list or a (possibly async) callable
ScopeChats = Callable[[], Any] | Sequence[int]

#: reads the current scope -> chats mapping. :meth:`CommandMenus.sync` takes
#: the *reader* rather than an already-resolved mapping, because the read is
#: part of the reconciliation and has to happen under the same lock as the
#: writes it feeds -- see there.
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


#: what we remember about a published chat menu; equality answers "does the
#: chat's menu already hold what this sync would write?"
_Fingerprint = tuple[tuple[str, str], ...]


def _fingerprint(commands: Sequence[BotCommand]) -> _Fingerprint:
    return tuple((c.command, c.description) for c in commands)


def _desired_menus(
    regs: Iterable[Registration], scope_chat_ids: Mapping[str, Sequence[int]]
) -> tuple[list[BotCommand], dict[int, list[BotCommand]]]:
    """The default menu, and the one merged menu each scoped chat should hold.

    A chat's menu is a function of the *set* of scopes that name it -- a
    resolver may report the same chat more than once (one backed by a SQL
    join does this routinely), and multiplicity must not change the menu.
    Scopes are merged in sorted-name order so the result -- and therefore the
    fingerprint that decides whether a write can be skipped -- is stable
    across syncs regardless of dict or listing order.

    Thousands of chats share a handful of distinct scope sets, so each menu is
    built once per set and shared by every chat that has it.
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
            menus[scopes] = _bot_commands([
                *default,
                *(reg for scope in sorted(scopes) for reg in by_scope.get(scope, ())),
            ])

    return _bot_commands(default), {
        chat_id: menus[scopes] for chat_id, scopes in chat_scopes.items()
    }


class CommandMenus:
    """Reconciles Telegram's per-chat command menus with the app's scopes.

    Holds the process's belief about remote state: a fingerprint per chat menu
    written, plus the ``known_scope_chats`` recovery seed. The seed names chats
    that *may* carry a menu from an earlier run -- a process has no memory of
    what a previous one published, so without it an admin demoted while the bot
    was down keeps their menu forever. It is loaded and consumed by the first
    **full** sync that *completes*: the first sync happens at startup, exactly
    when a database-backed seed or ``scope_chats`` resolver is most likely to
    be down, and a failed sync that ate the seed would orphan every historical
    chat for the life of the process. Chats whose individual API call fails
    stay seeded, so the next sync retries them.

    All syncing runs under one lock, and that lock covers reading the scopes
    as well as writing them. Serializing only the writes is not enough: two
    overlapping syncs would each read the world first, and whichever wrote
    *last* would win with whatever it read *first*, leaving a just-demoted
    admin holding the menu -- recorded as correct, so later syncs skip it.
    Hence :meth:`sync` takes a resolver it awaits itself rather than a mapping
    a caller resolved at some earlier, unknowable moment.
    """

    def __init__(self, known_chats: ScopeChats | None = None) -> None:
        self._published: dict[int, _Fingerprint] = {}
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

        ``resolve_scopes`` is awaited here, inside the lock, so that what a
        sync writes is what the world looked like when it took its turn. The
        lock therefore spans the resolvers' round trips and not just the
        writes -- deliberate, and the reason a resolver that hangs stalls
        every other sync rather than only its own caller.

        A full sync republishes the default menu (a single write, not worth
        tracking), writes every scoped chat whose menu would change, and
        deletes the menu of every chat that left all scopes. A targeted sync
        writes or deletes only the listed chats -- nothing else written,
        nothing else cleared, not even the default menu -- so it stays safe
        when a ``scope_chats`` resolver is misbehaving.

        A failed write for one chat is logged and leaves that chat's
        bookkeeping untouched, so the next sync retries it.
        """
        async with self._lock:
            default, desired = _desired_menus(regs, await resolve_scopes())
            if chats is None:
                if self._seed_source is not None:
                    self._seed |= set(await resolve_chats(self._seed_source))
                writes = set(desired)
                deletes = (set(self._published) | self._seed) - writes
                await tg_bot.set_my_commands(default, scope=BotCommandScopeDefault())
                # The seed source is consumed only now, with the default menu
                # written and the resolvers proven reachable: this sync will
                # run its per-chat loops, i.e. it "completes".
                self._seed_source = None
            else:
                listed = set(chats)
                writes = set(desired) & listed
                # Unconditional for listed chats: records can't distinguish
                # "never written" from "written by a previous process", and
                # clearing a chat with no menu is a no-op.
                deletes = listed - set(desired)

            for chat_id in sorted(writes):
                fingerprint = _fingerprint(desired[chat_id])
                if self._published.get(chat_id) == fingerprint:
                    self._seed.discard(chat_id)
                    continue
                try:
                    await tg_bot.set_my_commands(
                        desired[chat_id], scope=BotCommandScopeChat(chat_id=chat_id)
                    )
                except Exception as exc:  # noqa: BLE001 - one bad chat id
                    logger.warning("could not set commands for chat %s: %s", chat_id, exc)
                else:
                    self._published[chat_id] = fingerprint
                    self._seed.discard(chat_id)

            for chat_id in sorted(deletes):
                try:
                    await tg_bot.delete_my_commands(
                        scope=BotCommandScopeChat(chat_id=chat_id)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not clear commands for chat %s: %s", chat_id, exc)
                else:
                    self._published.pop(chat_id, None)
                    self._seed.discard(chat_id)
