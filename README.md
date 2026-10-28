# vitrine

[![CI](https://github.com/target111/vitrine/actions/workflows/ci.yml/badge.svg)](https://github.com/target111/vitrine/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/vitrine-tg)](https://pypi.org/project/vitrine-tg/)

A framework for building Telegram bots with [python-telegram-bot](https://python-telegram-bot.org/) that doesn't get in your way.

Handlers are just async functions. You get dependency injection, auth, robust message delivery, background workers, typed callbacks, rate limiting, and decent error handling -- the stuff every real bot needs. Everything from PTB stays accessible.

```python
from vitrine import Bot, Button, CallbackData, Screen

bot = Bot(token="...")


class MenuCB(CallbackData, prefix="menu"):
    section: str


@bot.command("start", description="Open the menu")
async def start(update):
    return Screen(
        text="Welcome!", keyboard=[[Button("Shop", callback=MenuCB(section="shop"))]]
    )


@bot.callback(MenuCB)
async def menu(data: MenuCB):  # decoded, validated payload injected
    return Screen(text=f"Section: {data.section}")


bot.run()
```

## Install

```bash
uv add vitrine-tg
# or
pip install vitrine-tg
```

The distribution is named `vitrine-tg`; the import name is `vitrine`.

## Local development

```bash
uv sync                                   # install deps (PTB 22, pydantic 2)
uv run pytest                             # run tests
BOT_TOKEN=... uv run python examples/small_bot.py
BOT_TOKEN=... uv run python examples/launcher_bot.py
BOT_TOKEN=... ADMIN_IDS=123 uv run python examples/shop/main.py
```

## Core features

### Dependency injection (`vitrine.injection`)

Handlers declare what they need by parameter name; the framework supplies it. Framework values (`update`, `context`, `bot`, `data`, `state`, `event`, `delivery`), your registered providers, command args, and middleware extras all live in one namespace:

```python
bot.provide_value("orders", OrderService(...))  # constants / singletons


@bot.provide("session")  # factories; may be async
async def session(db):  # ...and depend on each other
    async with db.begin() as s:
        yield s  # cleanup after handler


@bot.callback(OrderCB)
async def view_order(data: OrderCB, user: User, orders: OrderService, session): ...
```

Dependencies are resolved once per **update**: the ban gate, every handler group, conversation hooks and error handlers share one scope, and generator cleanups run after the update is fully processed. So the `user` your resolver loaded with `session` is used alongside that same, still-open `session`. Workers and lifecycle hooks get a fresh scope per call. Bad parameter names fail at startup, not production. `Depends(fn)` is available for explicit one-offs.

Annotations on injected parameters are documentation, not a contract -- duck-typed stand-ins are fine, and test suites rely on them. But an annotation that *provably* disagrees with its provider (`count: int` against a provider returning `str`) would surface later as an `AttributeError` deep inside a handler, so the build reports it on the `vitrine.build` logger. Only what a subclass test can settle is reported: protocols, generics, unions, unannotated factories, and `TYPE_CHECKING`-only names are left alone (`int` under a `float` annotation is fine too). `Bot(strict_types=True)` turns the warning into a build failure.

### Identity & auth (`vitrine.auth`)

You define the principal type. The framework handles resolve-once-per-update, caching, injection, and guards:

```python
auth = Auth(
    resolve_user, name="user", roles=lambda u: u.roles, is_banned=lambda u: u.banned
)
bot = Bot(token, auth=auth)


@bot.command("refund", scope="admin")
@requires("support")  # or @admin_only
async def refund(user: User, order_id: Arg[int]): ...


@bot.command("profile")
@requires_principal  # resolver returned None? -> "not registered" UX
async def profile(user: User): ...
```

Bans are enforced bot-wide. Guard failures turn into friendly error messages: a caller with no resolvable principal gets `NotRegisteredError` (point them at /start), one missing a role gets `NotAuthorizedError`. Any handler asking for `user` gets the same instance -- no re-fetching during an update, and the providers it was loaded with stay open until the update is done.

### Screens & delivery (`vitrine.screens`)

A `Screen` is a value object (text + keyboard + media + options) that doesn't need an `Update` -- unit-test your views by just calling them. `Delivery` sends it three ways: reply, edit, or proactively to any chat. It handles the annoying stuff:

- text↔media transitions send the new message first, then delete the old one
- all media types are detected (photo/video/animation/document/audio/voice/video-note/sticker)
- uploads are cached by content hash and re-sent as `file_id`; rejected IDs trigger exactly one retry
- "message is not modified" errors are silently skipped; `fresh=True` forces a new message anyway

Return a `Screen` from a handler and it renders automatically (edit for buttons, reply for commands). Or call `screen.render(update, context)` and `delivery.send(chat_id, screen)` manually.

Screens can also carry a **persistent reply keyboard** — the launcher pattern:

```python
LAUNCHER = ReplyKeyboard([["🛍 Shop", "ℹ️ Help"]])  # persistent by default


@bot.command("start")
async def start():
    return Screen(text="Welcome!", reply_keyboard=LAUNCHER)  # set once, sticks around


@bot.reply_button("🛍 Shop")  # presses route like messages
async def shop():
    return shop_screen()  # jump here from anywhere
```

A message carries an inline *or* a reply keyboard, never both, and Telegram can't attach reply keyboards to edits — `Delivery` turns such edits into replaces automatically. `Screen(reply_keyboard=REMOVE_REPLY_KEYBOARD)` takes the keyboard away. See `examples/launcher_bot.py`.

### Command menus (`vitrine.commands`)

`scope` on a command is a */help* grouping and a *menu* audience. `scope_chats` says which chats belong to each scope; vitrine keeps Telegram's per-chat command menus in sync with it, and `/help` shows a chat exactly the scopes its menu carries. (A scope is not access control -- guard the handler for that.)

```python
bot = Bot(
    token,
    scope_chats={
        "admin": get_admin_chat_ids,  # list or (async) callable
        "vip": get_vip_chat_ids,
    },
    known_scope_chats=get_all_chats_ever_scoped,  # recovery seed, see below
)


@bot.command("ban", scope="admin")
async def ban(user_id: Arg[int]): ...
```

`setMyCommands` writes to Telegram and *stays written*, so this is reconciliation, not fire-and-forget:

- Each chat gets **one** menu holding the default commands plus every scope that names it -- Telegram stores a single chat-scoped menu per chat, so a chat under both `admin` and `vip` sees both sets merged.
- A chat that leaves all scopes gets its menu **deleted**, dropping it back to the default menu. A demoted admin doesn't keep yesterday's menu.
- `await bot.sync_commands()` republishes on demand, so a promotion takes effect without a restart. Chats whose menu already holds what the sync would write are skipped, so a full re-sync after one promotion costs roughly one API call. `await bot.sync_commands(chats=[chat_id])` touches only those chats.
- The startup sync runs **in the background** -- the bot answers updates immediately, however many scoped chats there are -- and Telegram's flood control (`RetryAfter`) is waited out rather than skipped.
- `menu_store=` takes a `MenuStore` (`load`/`set`/`discard` of a per-chat fingerprint -- Redis, a table). The default lives in memory, so a restart re-sends each scoped menu once; a persistent store skips unchanged chats across restarts and remembers which chats hold a menu, so one demoted while the bot was down is still cleared.
- Without a persistent store, `known_scope_chats` names chats a *previous process* may have published to ("every chat any scope has ever resolved to"; clearing a chat with no menu is a no-op, so a generous list is safe). It's consumed by the first full sync that completes.

Without `scope_chats`, scopes only group `/help`, and `scope_member=(scope, principal) -> bool` (or, failing that, `Auth.is_admin`) decides who sees them -- so the two options are mutually exclusive. Configuration mistakes fail at build time: a `scope` no entry in `scope_chats` knows (almost always a typo), naming `"default"` in `scope_chats`, or a command name Telegram would reject -- which would otherwise freeze *every* menu, since Telegram rejects the whole batch.

### Lifecycle & workers (`vitrine.workers`)

```python
@bot.on_startup
async def warmup(delivery): ...


@bot.worker(every=30)
async def reconcile(orders, delivery):
    for o in await orders.confirmed():
        await delivery.send(o.chat_id, receipt_screen(o))


@bot.worker()
async def chain_watcher(feed): ...
```

Workers get DI, start after init, and shut down gracefully. Crashes restart automatically with exponential backoff -- no manual task supervision needed.

### Command arguments (`vitrine.args`)

Arguments are declared explicitly; every other parameter is injected:

```python
@bot.command("pay")
async def pay(user: User, wallet: WalletService, amount: Arg[float], note: Greedy = ""):
    ...  # /pay 5 thanks for lunch -> amount=5.0, note="thanks for lunch"
```

`Arg[T]` converts by calling `T` (type checkers see a plain `T`); `Greedy` takes the rest of the line. Required, optional and greedy arguments get an automatic usage line on bad input, and `/help pay` shows them. Because nothing is an argument by accident, a misspelled service name is a build error rather than a mystery argument, and registering a provider never changes a command's syntax.

### Handler precedence

PTB runs the first matching handler in each group. vitrine orders group 0 explicitly:

1. reply-keyboard buttons (`@bot.reply_button`) -- navigation works from anywhere, even mid-conversation;
2. conversations -- a caller's live run, then every entry point, see updates before any general handler, so a catch-all `@bot.message()` can't swallow the answer a step is waiting for, and a conversation's `/cancel` wins over a bot-wide `/cancel` only while a run is live;
3. every other handler, in registration order, then raw handlers;
4. a catch-all that answers button presses nothing claimed.

Handlers that could never run fail the build: the same command registered twice, or a callback handler behind one that takes every press of its model, including a conversation entry, since conversations come first (narrow it with `when=`). Registering anything after `build()` raises instead of being silently ignored.

## Features

| Feature | Module | Details |
|---|---|---|
| Typed callbacks | `callbacks` | Pydantic models with a prefix. Stale/corrupt data returns "button expired" instead of crashing. Keyed encoding (`keyed=True`) uses query strings and tolerates schema changes; `unpack()` auto-detects either format so live buttons survive upgrades. `when=` narrows a handler without swallowing the press; a press no handler takes is answered by a catch-all sitting last in the default group -- Telegram accepts one answer per press and PTB runs groups independently, so keep callback handlers in the default group or their alerts arrive too late to display. |
| Reply keyboards | `screens` | `ReplyKeyboard` value object (persistent + resized by default), `@bot.reply_button("label")` routes presses through the full pipeline, `ReplyButton(style=...)` takes the same styles as an inline `Button`, `REMOVE_REPLY_KEYBOARD` clears it. |
| Markdown builder | `markdown` | Composable/nestable nodes, safe escaping for V1+V2, `raw()` escape hatch. |
| Routers | `routing` | `@router.command/callback/message`, sub-routers, `router.raw()` for plain PTB handlers. Edited messages are ignored by default -- an edit redelivers the whole message and would re-run handlers -- with `edits=True` to opt a handler in. See *Handler precedence* above. |
| Command args | `args` | Explicit `Arg[T]` / `Greedy` parameters; required/optional/greedy; auto usage messages; everything else is injected. |
| Pagination | `pagination` | Implement `count()`/`fetch(offset, limit)`, use `Paginator` and `nav_row()` buttons. |
| Conversations | `conversations` | Dataclass state per run, string transitions, timeout, `on_exit(reason)` hooks (which may return a goodbye `Screen`). Steps return a state, `END` (finished), `CANCEL` (cancelled) or `None` (stay), optionally with a `Screen`. Entry commands land in `/help` and take `Arg[...]` arguments (bad args → usage line, and the run never starts); an entry that starts no run leaves no trace; `ANY_STATE` mounts one step on every state; `command="skip"` is valid only inside its state; `when=` narrows a step without swallowing the press; a caller has one live run per chat: its current state's steps get first look at their updates, whatever order conversations were registered in, and an update they don't take can start a conversation -- another one, or the same one again from the top. The new run replaces the old one *after* its first screen, dropping any goodbye `Screen` the old run's exit hook returns. Steps, their screens and exit hooks all run through the pipeline: DI, middleware, guards, error UX. `conv.active(chat_id=..., user_id=...)` inspects a live run. |
| Files/media | `media` | `download()` with timeout and cleanup; content-hash `file_id` cache shared with Screen rendering. |
| Rate limiting | `ratelimit` | `@throttle(3, per=60)`, custom keys and custom behavior on limits. |
| Logging | `logging` | Key=value format, one line per update, `audit()` convention. |
| Errors | `errors` | `@bot.on_error(Type)` registry dispatched by MRO. Handlers can return a `Screen` to render into the current message. |
| Command discovery | `commands` | Auto `/help` filtered to the scopes the caller's chat belongs to; `/help <command>` shows usage, arguments, the full docstring, guards and rate limit -- and a command the caller can't see answers exactly like one that doesn't exist. Per-chat menu sync with a pluggable `MenuStore`, flood control and `bot.sync_commands()`. `hidden=True` for internal handlers. |
| Middleware | `middleware` | `async def mw(event, call_next)` at bot or router scope. `event.extras` values become injectable. |

## Example: scaled mode

`examples/shop/` is a full app: a storefront with a **domain layer that never touches the bot layer** (`domain/`), views as pure functions (`domain -> Screen`), services injected into handlers, a `User` principal for guards and menus, a purchase conversation, admin commands, and a reconciler worker that messages buyers. Only `main.py` knows all the layers.

The order flow in `examples/shop/botapp/order_flow.py` shows the conversation features together: two entry points (a Buy button and a discoverable `/order <sku>` command with an `Arg[str]` argument), a `/skip` command scoped to one state, and a single Cancel handler mounted on `ANY_STATE` that returns `CANCEL`.

## Escape hatches

Not a fork or parallel dispatcher. `bot.build()` returns the PTB `Application` for webhooks (`bot.build(request=..., get_updates_request=...)` swaps its HTTP transports -- separate instances, since a long poll holds its connection); PTB's `run_polling`/`run_webhook` run vitrine's startup and shutdown for you, and `bot.startup()`/`stop()`/`shutdown()` are there if you drive the application some other way. `router.raw(handler, group=...)` registers plain PTB handlers. `Screen.extra` passes kwargs to PTB's send methods. Everything from PTB stays accessible.

## Testing

Views are pure functions -- test them without a live bot. `Screen.content()`/`markup()` show what would be sent.

For everything else, `vitrine.testing.TestClient` runs your bot through PTB's real dispatcher with Telegram faked at the HTTP layer -- handler groups, filters, the ban gate, conversations and error handlers all behave as in production, and every Bot API call is recorded:

```python
from vitrine.testing import TestClient, sent_texts


async def test_buying():
    async with TestClient(build_bot()) as client:
        await client.send("/start")
        await client.press(MenuCB(section="shop"))  # on the bot's latest message
        assert sent_texts(await client.send("2")) == ["Confirm your order"]
        assert client.calls("editMessageText")
```

`send`, `press`, `edit` and `feed` (any raw update) each return the API calls that one update produced; `FakeTelegram.fail(method, description, retry_after=...)` makes Telegram refuse a call. `TestClient(bot, startup=True)` also runs startup hooks, workers and the menu sync. A bot is built once, against one client's fake Telegram, so give each `TestClient` a fresh bot from a factory like `build_bot()`; reusing one raises. This repo's own suite -- including `tests/test_examples.py`, which buys something in the shop example -- is written this way.
