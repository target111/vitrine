"""Handlers written the way issue #1 describes -- deliberately not a test module.

PEP 563 turns every annotation here into a string, and ``Update`` is imported
only under ``TYPE_CHECKING``, so it cannot be resolved at runtime. That used to
make ``inspect.signature(fn, eval_str=True)`` raise ``NameError`` and degrade
*every* annotation in the signature to a string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vitrine.args import Greedy

if TYPE_CHECKING:  # never imported at runtime
    from telegram import Update


async def note(update: Update, tag: str, amount: int, rest: Greedy = Greedy("")):
    """The reported shape: an unresolvable injected param beside real args."""


async def unresolvable_argument(update: Update, target: Update):
    """``target`` is a command argument whose own type is TYPE_CHECKING-only."""
