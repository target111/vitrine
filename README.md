# vitrine

A framework for building Telegram bots with [python-telegram-bot](https://python-telegram-bot.org/) that doesn't get in your way.

Handlers are just async functions. You get dependency injection, auth, robust message delivery, background workers, typed callbacks, rate limiting, and decent error handling -- the stuff every real bot needs. Everything from PTB stays accessible.

```python
from vitrine import Bot, Button, CallbackData, Screen

bot = Bot(token="...")

class MenuCB(CallbackData, prefix="menu"):
    section: str

@bot.command("start", description="Open the menu")
async def start(update):
    return Screen(text="Welcome!", keyboard=[[Button("Shop", callback=MenuCB(section="shop"))]])

@bot.callback(MenuCB)
async def menu(data: MenuCB):          # decoded, validated payload injected
    return Screen(text=f"Section: {data.section}")

bot.run()
```

## Install

Not on PyPI yet -- install straight from GitHub:

```bash
uv add git+https://github.com/target111/vitrine
# or
pip install git+https://github.com/target111/vitrine
```

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
bot.provide_value("orders", OrderService(...))     # constants / singletons

@bot.provide("session")                            # factories; may be async
async def session(db):                             # ...and depend on each other
    async with db.begin() as s:
        yield s                                    # cleanup after handler

@bot.callback(OrderCB)
async def view_order(data: OrderCB, user: User, orders: OrderService, session):
    ...
```

Dependencies are resolved once per handler call. Bad parameter names fail at startup, not production. `Depends(fn)` is available for explicit one-offs.

### Identity & auth (`vitrine.auth`)

You define the principal type. The framework handles resolve-once-per-update, caching, injection, and guards:

```python
auth = Auth(resolve_user, name="user",
            roles=lambda u: u.roles, is_banned=lambda u: u.banned)
bot = Bot(token, auth=auth)

@bot.command("refund", scope="admin")
@requires("support")          # or @admin_only
async def refund(user: User, order_id: int): ...

@bot.command("profile")
@requires_principal          # resolver returned None? -> "not registered" UX
async def profile(user: User): ...
```

Bans are enforced bot-wide. Guard failures turn into friendly error messages: a caller with no resolvable principal gets `NotRegisteredError` (point them at /start), one missing a role gets `NotAuthorizedError`. Any handler asking for `user` gets the same instance -- no re-fetching during an update.

### Screens & delivery (`vitrine.screens`)

A `Screen` is a value object (text + keyboard + media + options) that doesn't need an `Update` -- unit-test your views by just calling them. `Delivery` sends it three ways: reply, edit, or proactively to any chat. It handles the annoying stuff:

- text↔media transitions send the new message first, then delete the old one
- all media types are detected (photo/video/animation/document/audio/voice/video-note/sticker)
- uploads are cached by content hash and re-sent as `file_id`; rejected IDs trigger exactly one retry
- "message is not modified" errors are silently skipped; `fresh=True` forces a new message anyway

Return a `Screen` from a handler and it renders automatically (edit for buttons, reply for commands). Or call `screen.render(update, context)` and `delivery.send(chat_id, screen)` manually.

Screens can also carry a **persistent reply keyboard** — the launcher pattern:

```python
LAUNCHER = ReplyKeyboard([["🛍 Shop", "ℹ️ Help"]])          # persistent by default

@bot.command("start")
async def start():
    return Screen(text="Welcome!", reply_keyboard=LAUNCHER)  # set once, sticks around

@bot.reply_button("🛍 Shop")                                 # presses route like messages
async def shop():
    return shop_screen()                                     # jump here from anywhere
```

A message carries an inline *or* a reply keyboard, never both, and Telegram can't attach reply keyboards to edits — `Delivery` turns such edits into replaces automatically. `Screen(reply_keyboard=REMOVE_REPLY_KEYBOARD)` takes the keyboard away. See `examples/launcher_bot.py`.

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

## Features

| Feature | Module | Details |
|---|---|---|
| Typed callbacks | `callbacks` | Pydantic models with a prefix. Stale/corrupt data returns "button expired" instead of crashing. Keyed encoding (`keyed=True`) uses query strings and tolerates schema changes; `unpack()` auto-detects either format so live buttons survive upgrades. |
| Reply keyboards | `screens` | `ReplyKeyboard` value object (persistent + resized by default), `@bot.reply_button("label")` routes presses through the full pipeline, `REMOVE_REPLY_KEYBOARD` clears it. |
| Markdown builder | `markdown` | Composable/nestable nodes, safe escaping for V1+V2, `raw()` escape hatch. |
| Routers | `routing` | `@router.command/callback/message`, sub-routers, `router.raw()` for plain PTB handlers. |
| Command args | `args` | Typed params from the signature; required/optional/`Greedy`; auto usage messages. |
| Pagination | `pagination` | Implement `count()`/`fetch(offset, limit)`, use `Paginator` and `nav_row()` buttons. |
| Conversations | `conversations` | Dataclass state per run, string transitions, timeout, `on_exit(reason)` hooks. Full DI/middleware/principal interop. |
| Files/media | `media` | `download()` with timeout and cleanup; content-hash `file_id` cache shared with Screen rendering. |
| Rate limiting | `ratelimit` | `@throttle(3, per=60)`, custom keys and custom behavior on limits. |
| Logging | `logging` | Key=value format, one line per update, `audit()` convention. |
| Errors | `errors` | `@bot.on_error(Type)` registry dispatched by MRO. Handlers can return a `Screen` to render into the current message. |
| Command discovery | `commands` | Auto `/help` filtered by caller's scopes. `set_my_commands()` per scope. `hidden=True` for internal handlers. |
| Middleware | `middleware` | `async def mw(event, call_next)` at bot or router scope. `event.extras` values become injectable. |

## Example: scaled mode

`examples/shop/` is a full app: a storefront with a **domain layer that never touches the bot layer** (`domain/`), views as pure functions (`domain -> Screen`), services injected into handlers, a `User` principal for guards and menus, a purchase conversation, admin commands, and a reconciler worker that messages buyers. Only `main.py` knows all the layers.

## Escape hatches

Not a fork or parallel dispatcher. `bot.build()` returns the PTB `Application` for webhooks. `router.raw(handler, group=...)` registers plain PTB handlers. `Screen.extra` passes kwargs to PTB's send methods. Everything from PTB stays accessible.

## Testing

Views are pure functions -- test them without a live bot. `Screen.content()`/`markup()` show what would be sent. `Delivery` accepts any object with `send`/`edit` methods (see `tests/conftest.py` for a mock). This repo has 95 tests that exercise dispatch and conversations the same way you can.
