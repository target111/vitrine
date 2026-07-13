"""A persistent reply-keyboard launcher.

Run:  BOT_TOKEN=123:abc uv run python examples/launcher_bot.py

/start pins a launcher under the input field once; from then on, tapping
"🛍 Shop" / "👤 Profile" / "ℹ️ Help" bootstraps the user into that screen from
anywhere — mid-scroll, days later, no slash commands needed. Inline keyboards
still drive navigation *inside* each screen; the launcher rides along
untouched because reply and inline keyboards live in different places.
"""

from __future__ import annotations

import os

from vitrine import (
    REMOVE_REPLY_KEYBOARD,
    Bot,
    Button,
    CallbackData,
    ReplyKeyboard,
    Screen,
    setup_logging,
)
from vitrine.markdown import Md, bold, code, italic

bot = Bot(token=os.environ.get("BOT_TOKEN", ""))

SHOP, PROFILE, HELP = "🛍 Shop", "👤 Profile", "ℹ️ Help"

LAUNCHER = ReplyKeyboard(
    [[SHOP, PROFILE], [HELP]],
    placeholder="Where to?",
)

CATALOG = {"teapot": "A stout little teapot. 12 €", "globe": "The whole world. 30 €"}


class ItemCB(CallbackData, prefix="item"):
    name: str


class HomeCB(CallbackData, prefix="home"):
    pass


# --------------------------------------------------------------------- screens


def shop_screen() -> Screen:
    return Screen(
        text=Md()
        .heading(SHOP)
        .line(italic("Inline buttons navigate the screen; the launcher stays put.")),
        keyboard=[
            [Button(name.title(), callback=ItemCB(name=name)) for name in CATALOG]
        ],
    )


def help_screen() -> Screen:
    return Screen(
        text=Md()
        .heading(HELP)
        .line("The keyboard below the input field is ", bold("persistent"), ".")
        .line("Tap a button on it from anywhere to jump to that screen.")
        .line("Hide it with ", code("/hide"), "; ", code("/start"), " brings it back."),
    )


# --------------------------------------------------------------------- commands


@bot.command("start", description="Pin the launcher and say hi")
async def start(update):
    """Sets the reply keyboard exactly once; it persists across every message."""
    return Screen(
        text=Md()
        .line("Welcome, ", bold(update.effective_user.first_name), "!")
        .blank()
        .line("Use the launcher below — it works from anywhere."),
        reply_keyboard=LAUNCHER,
    )


@bot.command("hide", description="Remove the launcher")
async def hide():
    return Screen(
        text="Launcher removed — /start brings it back.",
        reply_keyboard=REMOVE_REPLY_KEYBOARD,
    )


# --------------------------------------------------------------------- launcher

# Presses are plain text messages matching the label; the full pipeline
# (DI, guards, middleware) applies, and a returned Screen replies as usual.


@bot.reply_button(SHOP)
async def open_shop():
    return shop_screen()


@bot.reply_button(PROFILE)
async def open_profile(update):
    tg_user = update.effective_user
    return Screen(
        text=Md()
        .heading(PROFILE)
        .line("Name: ", bold(tg_user.first_name))
        .line("Id: ", code(tg_user.id)),
    )


@bot.reply_button(HELP)
async def open_help():
    return help_screen()


# --------------------------------------------------------------------- callbacks


@bot.callback(ItemCB)
async def view_item(data: ItemCB):
    return Screen(
        text=Md().heading(data.name.title()).line(CATALOG[data.name]),
        keyboard=[[Button("« Back to shop", callback=HomeCB())]],
    )


@bot.callback(HomeCB)
async def back_to_shop():
    return shop_screen()


if __name__ == "__main__":
    setup_logging()
    if not bot.token:
        raise SystemExit("Set BOT_TOKEN first.")
    bot.run()
