# vitrine

A batteries-included framework on top of [python-telegram-bot](https://python-telegram-bot.org/)
for inline-keyboard-driven bots — **thin in ceremony, deep in capability**.

Handlers stay plain async functions and PTB's dispatcher/filters stay fully
reachable; the framework carries the plumbing every serious bot re-implements:
dependency injection, identity resolution, robust message delivery, background
worker supervision, typed callbacks, rate limits, and error UX.

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

## Install & run

```bash
uv sync                                          # deps (PTB 22, pydantic 2)
uv run pytest                                    # test suite
BOT_TOKEN=... uv run python examples/small_bot.py            # small mode
BOT_TOKEN=... ADMIN_IDS=123 uv run python examples/shop/main.py  # scaled mode
```

## The foundation primitives

### Dependency injection (`vitrine.injection`)

Handlers declare what they need by parameter name; the framework supplies it.
Framework values (`update`, `context`, `bot`, `data`, `state`, `event`,
`delivery`), registered providers, parsed command args, and middleware extras
all unify in one namespace:

```python
bot.provide_value("orders", OrderService(...))     # constants / singletons

@bot.provide("session")                            # factories; may be async
async def session(db):                             # ...and depend on each other
    async with db.begin() as s:
        yield s                                    # generator -> cleanup after handler

@bot.callback(OrderCB)
async def view_order(data: OrderCB, user: User, orders: OrderService, session):
    ...
```

Resolution is cached per invocation; unknown parameters fail **at build time**,
not in production. `Depends(fn)` is available for explicit one-offs.

### Identity: an injectable principal (`vitrine.auth`)

The app owns the principal type; the framework owns resolve-once-per-update,
caching, injection, and guards:

```python
auth = Auth(resolve_user, name="user",
            roles=lambda u: u.roles, is_banned=lambda u: u.banned)
bot = Bot(token, auth=auth)

@bot.command("refund", scope="admin")
@requires("support")          # or @admin_only
async def refund(user: User, order_id: int): ...
```

Bans are enforced bot-wide (a gate in group -100), guard failures become
friendly UX via the error layer, and any handler asking for `user` gets the
same resolved object — never re-queried within an update.

### Screens & delivery (`vitrine.screens`)

A `Screen` is a value object (text + keyboard + media + options) constructible
with **no Update in hand** — unit-test your views by calling them. One
`Delivery` service sends it three ways: reply, edit-in-place, or proactively
to any chat id. Delivery is robust by design:

- text↔media transitions **send the replacement before deleting** the old message;
- all existing media kinds are detected (photo/video/animation/document/audio/voice/video-note/sticker);
- uploads are cached by content hash and re-sent as `file_id`; a rejected id
  triggers exactly one re-upload;
- "message is not modified" is swallowed; `fresh=True` forces a new message.

Returning a `Screen` from a handler renders it automatically (edit for button
presses, reply for commands). `screen.render(update, context)` and
`delivery.send(chat_id, screen)` are the explicit paths.

### Lifecycle & workers (`vitrine.workers`)

```python
@bot.on_startup
async def warmup(delivery): ...

@bot.worker(every=30)                 # periodic job
async def reconcile(orders, delivery):
    for o in await orders.confirmed():
        await delivery.send(o.chat_id, receipt_screen(o))

@bot.worker()                          # long-running supervised loop
async def chain_watcher(feed): ...
```

Workers get DI, start after init, cancel gracefully on shutdown, and restart
with exponential backoff on crash — no hand-rolled task supervision.

## Feature tour

| Feature | Module | In short |
|---|---|---|
| Typed callbacks | `callbacks` | pydantic models with a prefix; stale/corrupt data answers "button expired" instead of crashing; opt-in keyed encoding (`keyed=True` → `menu?section=shop&page=2`) omits defaults and tolerates schema changes, and `unpack` auto-detects either format so live buttons survive the switch |
| Markdown builder | `markdown` | composable/nestable nodes, safe escaping for V1+V2, `raw()` escape hatch |
| Routers | `routing` | `@router.command/callback/message`, sub-routers, `router.raw()` for plain PTB handlers |
| Command args | `args` | typed params from the signature; required/optional/`Greedy`; auto usage messages |
| Pagination | `pagination` | `count()`/`fetch(offset, limit)` protocol, `Paginator`, `nav_row()` buttons |
| Conversations | `conversations` | dataclass state per run, string transitions, timeout, `on_exit(reason)`; full DI/middleware/principal interop |
| Files/media | `media` | `download()` with timeout + temp cleanup; content-hash `file_id` cache shared with Screen rendering |
| Rate limiting | `ratelimit` | `@throttle(3, per=60)`, custom keys and on-limit behaviour |
| Logging | `logging` | key=value lines; one line per handled update; `audit()` convention |
| Errors | `errors` | `@bot.on_error(Type)` registry dispatched by MRO; handlers may return a `Screen` rendered into the current message |
| Command discovery | `commands` | auto-`/help` filtered by caller's scopes; `set_my_commands` per scope (`scope_chats={"admin": [...]}`); `hidden=True` for internal handlers |
| Middleware | `middleware` | `async def mw(event, call_next)` at bot or router scope; `event.extras` values become injectable |

## Scaled mode

`examples/shop/` is the reference app: a storefront with a **domain layer that
never imports the bot layer** (`domain/`), views as pure `domain -> Screen`
functions, services injected into handlers, a rich `User` principal used
across menus and guards, a guided purchase conversation, admin-scoped
commands, and a payment reconciler worker that proactively messages buyers.
`main.py` is the only file that knows every layer.

## Escape hatches

Not a fork, not a parallel dispatcher: `bot.build()` returns the PTB
`Application` (use it for webhooks), `router.raw(handler, group=...)` registers
untouched PTB handlers, `Screen.extra` passes kwargs through to PTB send
methods, and everything PTB remains importable next to vitrine.

## Testing your bot

Everything that computes a message or a decision runs without a live bot:
views are pure functions, `Screen.content()`/`markup()` expose what would be
sent, `Delivery` accepts any object with PTB's send/edit methods (see
`tests/conftest.py` for a recording fake), and conversations/dispatch are
exercised in this repo's 70 tests the same way your app can.
