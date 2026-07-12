"""Small mode: a single-file bot touching every vitrine feature.

Run:  BOT_TOKEN=123:abc [ADMIN_ID=your_id] uv run python examples/small_bot.py

Features on display: typed callbacks, Screens with edit/reply/media handling,
markdown builder, DI providers, an in-memory principal with roles/bans and an
explicit /start registration gate, typed command args, pagination, a guided
conversation, rate limiting, a background worker that proactively messages
subscribers, error UX, and auto-/help.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

from vitrine import (
    END,
    Auth,
    Bot,
    Button,
    CallbackData,
    Conversation,
    ExitReason,
    Greedy,
    ListSource,
    Paginator,
    Screen,
    UserFacingError,
    admin_only,
    nav_row,
    requires,
    requires_principal,
    setup_logging,
    throttle,
)
from vitrine.markdown import Md, bold, code, italic, link

# --------------------------------------------------------------------- identity


@dataclass
class Profile:
    """The app-defined principal: vitrine never dictates this shape."""

    id: int
    name: str
    roles: set[str] = field(default_factory=set)
    banned: bool = False
    clicks: int = 0


PROFILES: dict[int, Profile] = {}
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))


async def resolve_profile(update) -> Profile | None:
    """Lookup only — registration is explicit, in /start. Handlers that need a
    profile are marked @requires_principal; unregistered callers get told to
    /start instead of a crash or a misleading "not authorized"."""
    tg_user = getattr(update, "effective_user", None)
    if tg_user is None:
        return None

    return PROFILES.get(tg_user.id)


bot = Bot(
    token=os.environ.get("BOT_TOKEN", ""),
    auth=Auth(
        resolve_profile,
        name="user",
        roles=lambda p: p.roles,
        is_banned=lambda p: p.banned,
    ),
    scope_chats={"admin": lambda: [ADMIN_ID] if ADMIN_ID else []},
)

STARTED_AT = time.monotonic()
SUBSCRIBERS: set[int] = set()


# --------------------------------------------------------------------- providers


@bot.provide("fortunes")
def fortunes() -> list[str]:
    return [
        "You will refactor something today.",
        "A merge conflict approaches.",
        "Beware of the untested branch.",
        "Your CI shall be green.",
        "An off-by-one error hides nearby.",
    ]


# --------------------------------------------------------------------- middleware


@bot.middleware
async def clicks_counter(event, call_next):
    """Cross-cutting concern: count button presses per user (extras -> injectable)."""
    profile = PROFILES.get(getattr(event.update.effective_user, "id", 0))
    if profile is not None and event.data is not None:
        profile.clicks += 1

    return await call_next(event)


# --------------------------------------------------------------------- callbacks


class MenuCB(CallbackData, prefix="m", keyed=True):  # wire format: m?section=about
    section: str


class FortuneCB(CallbackData, prefix="f"):
    reroll: bool = False


class PageCB(CallbackData, prefix="pg"):
    page: int = 1


class SubCB(CallbackData, prefix="sub"):
    on: bool


# --------------------------------------------------------------------- screens


def main_menu(user: Profile) -> Screen:
    text = (
        Md()
        .heading("Demo Bot")
        .line(
            "Hello, ",
            bold(user.name),
            "! You have pressed ",
            code(user.clicks),
            " buttons.",
        )
        .blank()
        .line(italic("Pick a section:"))
    )

    return Screen(
        text=text,
        keyboard=[
            [
                Button("🔮 Fortune", callback=FortuneCB()),
                Button("📜 List", callback=PageCB()),
            ],
            [
                Button(
                    "🔕 Unsubscribe" if user.id in SUBSCRIBERS else "🔔 Subscribe",
                    callback=SubCB(on=user.id not in SUBSCRIBERS),
                )
            ],
            [Button("ℹ️ About", callback=MenuCB(section="about"))],
        ],
    )


# --------------------------------------------------------------------- commands


@bot.command("start", description="Register and open the main menu")
async def start(update):
    """The one unguarded entry point: registers the caller, then shows the menu."""
    tg_user = update.effective_user
    profile = PROFILES.get(tg_user.id)
    if profile is None:
        profile = PROFILES[tg_user.id] = Profile(id=tg_user.id, name=tg_user.first_name)
        if tg_user.id == ADMIN_ID:
            profile.roles.add("admin")

    return main_menu(profile)


@bot.command("me", description="Show your profile")
@requires_principal  # unregistered callers get NotRegisteredError UX, not a crash
async def me(user: Profile):
    doc = (
        Md()
        .heading("Your profile")
        .line("Name: ", bold(user.name))
        .line("Roles: ", code(", ".join(sorted(user.roles)) or "none"))
        .line("Buttons pressed: ", code(user.clicks))
    )
    return Screen(text=doc)


@bot.command("add", description="Add two numbers")
async def add(a: float, b: float):
    """Typed args: /add 2 40 — arity/type errors produce automatic usage help."""
    return Screen(text=Md().line(code(a), " + ", code(b), " = ", bold(str(a + b))))


@bot.command("echo", description="Echo text back, safely escaped")
async def echo(text_arg: Greedy):
    return Screen(text=Md().line("You said: ", bold(text_arg)))


@bot.command("roll", description="Roll a die (rate limited)")
@throttle(3, per=30)
async def roll(update):
    await update.effective_message.reply_text(f"🎲 {random.randint(1, 6)}")


@bot.command("crash", description="See friendly error UX", hidden=True)
async def crash():
    raise InsufficientKarma(needed=9000)


@bot.command("ban", description="Ban a user by id", scope="admin")
@admin_only
async def ban(target_id: int, user: Profile):
    PROFILES.setdefault(target_id, Profile(id=target_id, name="?")).banned = True
    return Screen(text=Md().line("Banned ", code(target_id), "."))


@bot.command("promote", description="Grant a role", scope="admin")
@requires("admin")
async def promote(target_id: int, role: str = "moderator"):
    PROFILES.setdefault(target_id, Profile(id=target_id, name="?")).roles.add(role)
    return Screen(text=Md().line("Gave ", code(target_id), " the ", bold(role), " role."))


# --------------------------------------------------------------------- callbacks


@bot.callback(MenuCB, when=lambda d: d.section == "about")
async def about(update, context):
    text = (
        Md()
        .heading("About")
        .line("Built with ", link("vitrine", "https://example.invalid/vitrine"), ".")
        .line("Screens, typed callbacks, DI, workers — all in one file.")
    )
    return Screen(text=text, keyboard=[[Button("« Back", callback=MenuCB(section="home"))]])


@bot.callback(MenuCB, when=lambda d: d.section == "home")
@requires_principal  # a stale keyboard can outlive the in-memory profile store
async def back_home(user: Profile):
    return main_menu(user)


@bot.callback(FortuneCB)
async def fortune(fortunes: list[str]):
    return Screen(
        text=Md().heading("🔮 Fortune").line(italic(random.choice(fortunes))),
        keyboard=[
            [Button("Again", callback=FortuneCB(reroll=True))],
            [Button("« Back", callback=MenuCB(section="home"))],
        ],
    )


@bot.callback(PageCB)
async def paged_list(data: PageCB):
    page = await Paginator(ListSource([f"Item #{i}" for i in range(1, 48)]), 7).page(
        data.page
    )
    doc = Md().heading(f"Items — page {page.number}/{page.pages}")
    for item in page.items:
        doc.bullet(item)

    return Screen(
        text=doc,
        keyboard=[
            nav_row(page, lambda n: PageCB(page=n)),
            [Button("« Back", callback=MenuCB(section="home"))],
        ],
    )


@bot.callback(SubCB)
@requires_principal
async def subscribe(data: SubCB, user: Profile):
    (SUBSCRIBERS.add if data.on else SUBSCRIBERS.discard)(user.id)
    return main_menu(user)


# --------------------------------------------------------------------- conversation


@dataclass
class FeedbackState:
    topic: str | None = None
    body: str | None = None


feedback = Conversation("feedback", FeedbackState, timeout=120)


@feedback.entry(command="feedback")
@requires_principal  # guards work on conversation entries too
async def feedback_start(state: FeedbackState):
    return "topic", Screen(text="What is your feedback about? (or /cancel)")


@feedback.state("topic")
async def feedback_topic(state: FeedbackState, update):
    state.topic = update.effective_message.text
    return "body", Screen(text=f"Got it — “{state.topic}”. Tell me more:")


@feedback.state("body")
async def feedback_body(state: FeedbackState, update, user: Profile):
    state.body = update.effective_message.text
    return END, Screen(
        text=Md().line(
            "Thanks, ", bold(user.name), "! Filed under ", code(state.topic), "."
        )
    )


@feedback.cancel()
async def feedback_cancel(update):
    await update.effective_message.reply_text("Cancelled.")


@feedback.on_exit
async def feedback_exit(state: FeedbackState, reason: ExitReason):
    print(f"feedback conversation ended: {reason} topic={state.topic!r}")


bot.conversation(feedback)


# --------------------------------------------------------------------- worker


@bot.worker(every=300, initial_delay=60)
async def uptime_pusher(delivery):
    """Proactively messages subscribers — no Update anywhere in sight."""
    minutes = int((time.monotonic() - STARTED_AT) / 60)
    screen = Screen(text=Md().line("⏱ Bot uptime: ", bold(f"{minutes} min")))
    for chat_id in list(SUBSCRIBERS):
        await delivery.send(chat_id, screen)


# --------------------------------------------------------------------- errors


class InsufficientKarma(UserFacingError):
    def __init__(self, needed: int) -> None:
        super().__init__(f"You need {needed} karma for that.")
        self.needed = needed


@bot.on_error(InsufficientKarma)
async def karma_ux(error: InsufficientKarma):
    return Screen(
        text=Md().heading("Not enough karma").line("Required: ", code(error.needed)),
        keyboard=[[Button("« Menu", callback=MenuCB(section="home"))]],
    )


# --------------------------------------------------------------------- lifecycle


@bot.on_startup
async def announce():
    print("small_bot is up.")


@bot.on_shutdown
async def farewell():
    print("small_bot is down.")


if __name__ == "__main__":
    setup_logging()
    if not bot.token:
        raise SystemExit("Set BOT_TOKEN (and optionally ADMIN_ID) first.")
    bot.run()
