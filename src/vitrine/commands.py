"""Command discovery: auto-generated /help and per-scope command menus.

Commands carry metadata at registration (``description``, ``scope``,
``hidden``). From that the framework derives:

- a ``/help`` screen listing what the *caller* can see (admins see admin
  commands), with ``hidden=True`` handlers (entry-only, internal) left out;
- Telegram command menus via ``set_my_commands``: the default scope gets the
  default commands, and each named scope's chats (e.g. every admin's private
  chat) get the default + scoped commands.

Publishing a menu is a write to Telegram, not to the bot: every scope stays as
last written until it is overwritten or deleted, so :func:`sync_command_menus`
also has to *unpublish* the chats that have left a scope.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence, Set
from typing import Any

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from .markdown import Md, code
from .routing import Registration
from .screens import Screen

logger = logging.getLogger("vitrine.commands")


def _command_regs(regs: Iterable[Registration]) -> list[Registration]:
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


def help_screen(regs: Iterable[Registration], visible_scopes: set[str]) -> Screen:
    doc = Md().heading("Available commands")
    by_scope: dict[str, list[Registration]] = {}
    for reg in _command_regs(regs):
        if reg.scope in visible_scopes:
            by_scope.setdefault(reg.scope, []).append(reg)

    for scope in sorted(by_scope, key=lambda s: (s != "default", s)):
        if scope != "default":
            doc.blank().heading(scope.capitalize())
        for reg in by_scope[scope]:
            doc.line(code(f"/{reg.command}"), " — ", reg.description or reg.command or "")

    return Screen(text=doc)


def _bot_commands(regs: Sequence[Registration]) -> list[BotCommand]:
    return [
        BotCommand(reg.command or "", (reg.description or reg.command or "")[:256])
        for reg in regs
    ]


async def sync_command_menus(
    tg_bot: Any,
    regs: Iterable[Registration],
    scope_chat_ids: dict[str, Sequence[int]],
    *,
    published_chats: Set[int] = frozenset(),
) -> set[int]:
    """Publish the default command menu plus per-scope menus for given chats.

    ``published_chats`` are the chats that may still carry a chat-scoped menu
    from an earlier sync; the same set, updated, comes back for the next one.

    Telegram resolves a private chat's menu as chat scope, then all-private
    chats, then default, and keeps each scope until something replaces it. A
    chat that drops out of ``scope_chat_ids`` -- an admin demoted, a resolver
    that came back empty -- therefore keeps the menu it was last given for
    good, shadowing the default one and never showing a command added since.
    Those chats get their menu deleted here, which drops them back to the
    default menu.
    """
    commands = _command_regs(regs)
    default = [reg for reg in commands if reg.scope == "default"]
    await tg_bot.set_my_commands(_bot_commands(default), scope=BotCommandScopeDefault())

    known = set(published_chats)
    scoped_chats: set[int] = set()
    for scope, chat_ids in scope_chat_ids.items():
        scoped = default + [reg for reg in commands if reg.scope == scope]
        for chat_id in chat_ids:
            scoped_chats.add(chat_id)
            try:
                await tg_bot.set_my_commands(
                    _bot_commands(scoped), scope=BotCommandScopeChat(chat_id=chat_id)
                )
            except Exception as exc:  # a bad chat id must not break startup
                logger.warning(
                    "could not set %r commands for chat %s: %s", scope, chat_id, exc
                )
            else:
                known.add(chat_id)

    # Sorted so a failure part-way through leaves a reproducible state.
    for chat_id in sorted(known - scoped_chats):
        try:
            await tg_bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception as exc:  # retried on the next sync, since it stays known
            logger.warning("could not clear the menu of chat %s: %s", chat_id, exc)
        else:
            known.discard(chat_id)

    logger.info(
        "command menus synced: %d default, %d chat-scoped", len(default), len(scoped_chats)
    )

    return known
