"""Handlers written the way issue #1 describes -- deliberately not a test module.

PEP 563 turns every annotation here into a string, and ``Update`` is imported
only under ``TYPE_CHECKING``, so it cannot be resolved at runtime; the
arguments beside it must still be found and converted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vitrine.args import Arg, Greedy

if TYPE_CHECKING:  # never imported at runtime
    from telegram import Update


async def note(update: Update, tag: Arg[str], amount: Arg[int], rest: Greedy = Greedy("")):
    """The reported shape: an unresolvable injected param beside real args."""


async def unresolvable_argument(update: Update, target: Arg[Update]):
    """``target`` is a command argument whose own type is TYPE_CHECKING-only."""


def local_argument_type():
    """A handler whose argument type is defined inside the function that builds it."""
    from enum import Enum

    class Color(Enum):
        RED = "red"

    async def paint(color: Arg[Color]):
        """``Color`` is not a module global, so the string can't be evaluated."""

    return paint
