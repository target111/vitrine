# vitrine

[![CI](https://github.com/target111/vitrine/actions/workflows/ci.yml/badge.svg)](https://github.com/target111/vitrine/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/vitrine-tg)](https://pypi.org/project/vitrine-tg/)

**Your handler returns a screen. vitrine does the rest.**

A framework for button-driven Telegram bots on top of [python-telegram-bot](https://python-telegram-bot.org/).

```python
from vitrine import Bot, Button, CallbackData, Screen

bot = Bot(token="...")


class MenuCB(CallbackData, prefix="menu"):
    section: str


@bot.command("start", description="Open the menu")
async def start():
    return Screen(
        text="Welcome!", keyboard=[[Button("Shop", callback=MenuCB(section="shop"))]]
    )


@bot.callback(MenuCB)
async def menu(data: MenuCB):  # decoded, validated button data
    return Screen(text=f"Section: {data.section}")


bot.run()
```

Also included: dependency injection, auth, conversations, background workers, per-chat command menus and a test client. PTB stays underneath and fully accessible.

## Install

```bash
uv add vitrine-tg
# or
pip install vitrine-tg
```

The package is named `vitrine-tg` on PyPI; the import name is `vitrine`. It needs Python 3.11 or newer.

## A short tour

**Screens.** A handler returns a `Screen` (text, buttons, optional media) and vitrine sends it. A button press edits the message it came from, and a command gets a reply. Switching between text and media, re-sending uploads by `file_id`, and persistent reply keyboards are handled for you. [Screens & delivery](https://github.com/target111/vitrine/blob/main/docs/screens.md)

**Typed buttons.** Button data is a pydantic model with a short prefix, so handlers get validated fields instead of strings to parse. A stale or corrupt button tells the user it has expired instead of raising an error. [Callbacks](https://github.com/target111/vitrine/blob/main/docs/callbacks.md)

**Dependency injection.** Handlers ask for what they need by parameter name. Providers can be values, functions or async generators, and are resolved once per update:

```python
bot.provide_value("db", Database(DB_URL))


@bot.provide("session")
async def session(db):  # providers can depend on other providers
    async with db.begin() as s:
        yield s  # cleaned up once the update is handled


@bot.command("orders")
async def list_orders(update, session): ...
```

A parameter nothing can supply fails at startup, not when a user hits it. [Dependency injection](https://github.com/target111/vitrine/blob/main/docs/dependency-injection.md)

**Command arguments.** Arguments are declared with `Arg[T]`, and everything else is injected. Bad input gets a usage line back:

```python
from vitrine import Arg, Greedy


@bot.command("pay")
async def pay(wallet, amount: Arg[float], note: Greedy = ""):
    ...  # "/pay 5 thanks for lunch" gives amount=5.0, note="thanks for lunch"
```

[Commands & /help](https://github.com/target111/vitrine/blob/main/docs/commands.md)

**Auth.** You define the user type and how to load it. vitrine loads it once per update, injects it, enforces bans and checks role guards:

```python
from vitrine import Arg, Auth, Bot, requires

auth = Auth(load_user, name="user", roles=lambda u: u.roles, is_banned=lambda u: u.banned)
bot = Bot(token, auth=auth)


@bot.command("refund")
@requires("support")
async def refund(user, order_id: Arg[int]): ...
```

[Auth](https://github.com/target111/vitrine/blob/main/docs/auth.md)

**Conversations.** Multi-step flows keep a dataclass of state per user and move between named states:

```python
from dataclasses import dataclass

from vitrine import END, Conversation, Screen


@dataclass
class Order:
    item: str = ""


order = Conversation("order", Order, timeout=300)


@order.entry(command="order")
async def begin():
    return "item", Screen(text="What would you like?")


@order.state("item")
async def got_item(state: Order, update):
    state.item = update.effective_message.text
    return END, Screen(text=f"Ordered {state.item}.")


bot.conversation(order)
```

Each user has at most one conversation per chat, and starting one cancels the previous one. [Conversations](https://github.com/target111/vitrine/blob/main/docs/conversations.md)

**Command menus and /help.** `/help` is generated from your commands. Give commands a `scope` and map scopes to chats, and vitrine keeps each chat's Telegram command menu in sync, including clearing it when a chat drops out of every scope. [Command menus](https://github.com/target111/vitrine/blob/main/docs/command-menus.md)

**Background workers.** Workers get the same injection as handlers and restart with backoff if they crash:

```python
@bot.worker(every=60)
async def send_reminders(reminders, delivery):
    for reminder in await reminders.due():
        await delivery.send(reminder.chat_id, Screen(text=reminder.text))
```

[Workers & lifecycle](https://github.com/target111/vitrine/blob/main/docs/workers.md)

**Testing.** `TestClient` runs your bot through PTB's real dispatcher with Telegram faked at the HTTP layer, and records every API call:

```python
from vitrine.testing import TestClient


async def test_menu():
    async with TestClient(build_bot()) as client:
        await client.send("/start")
        await client.press(MenuCB(section="shop"))
        assert client.last_text() == "Section: shop"
```

[Testing](https://github.com/target111/vitrine/blob/main/docs/testing.md)

**Also included:** [middleware and error handlers](https://github.com/target111/vitrine/blob/main/docs/middleware-and-errors.md), and [rate limiting, pagination, a Markdown builder, structured logging and file downloads](https://github.com/target111/vitrine/blob/main/docs/utilities.md).

**PTB is still there.** `bot.build()` returns the PTB `Application`, so webhooks and anything else PTB supports work as usual, and `bot.raw()` registers plain PTB handlers. [PTB integration](https://github.com/target111/vitrine/blob/main/docs/ptb-integration.md)

## Examples

- [`examples/small_bot.py`](https://github.com/target111/vitrine/blob/main/examples/small_bot.py): a single-file bot that uses most features.
- [`examples/launcher_bot.py`](https://github.com/target111/vitrine/blob/main/examples/launcher_bot.py): a persistent reply-keyboard launcher.
- [`examples/shop/`](https://github.com/target111/vitrine/tree/main/examples/shop): a larger app with a domain layer kept separate from the bot layer, an order conversation, admin commands and a worker.

## Documentation

- [Dependency injection](https://github.com/target111/vitrine/blob/main/docs/dependency-injection.md)
- [Auth](https://github.com/target111/vitrine/blob/main/docs/auth.md)
- [Screens & delivery](https://github.com/target111/vitrine/blob/main/docs/screens.md)
- [Callbacks](https://github.com/target111/vitrine/blob/main/docs/callbacks.md)
- [Commands & /help](https://github.com/target111/vitrine/blob/main/docs/commands.md)
- [Command menus](https://github.com/target111/vitrine/blob/main/docs/command-menus.md)
- [Conversations](https://github.com/target111/vitrine/blob/main/docs/conversations.md)
- [Handler precedence](https://github.com/target111/vitrine/blob/main/docs/handler-precedence.md)
- [Workers & lifecycle](https://github.com/target111/vitrine/blob/main/docs/workers.md)
- [Middleware & errors](https://github.com/target111/vitrine/blob/main/docs/middleware-and-errors.md)
- [Utilities](https://github.com/target111/vitrine/blob/main/docs/utilities.md)
- [PTB integration](https://github.com/target111/vitrine/blob/main/docs/ptb-integration.md)
- [Testing](https://github.com/target111/vitrine/blob/main/docs/testing.md)
- [Changelog](https://github.com/target111/vitrine/blob/main/CHANGELOG.md)

## Local development

```bash
uv sync          # install dependencies
uv run pytest    # run the tests
BOT_TOKEN=... uv run python examples/small_bot.py
```

## License

MIT
