# PTB integration

vitrine is not a fork of python-telegram-bot and doesn't replace its dispatcher. It registers ordinary PTB handlers on an ordinary `Application`, and everything PTB offers stays available.

## Running the bot

`bot.run()` builds the application and starts long polling. For anything else, `bot.build()` returns the PTB `Application`:

```python
from vitrine import Bot

bot = Bot(token="...")
# ... register handlers ...

application = bot.build()
application.run_webhook(
    listen="0.0.0.0",
    port=8443,
    url_path="telegram",
    webhook_url="https://example.com/telegram",
)
```

PTB's `run_polling` and `run_webhook` run vitrine's startup and shutdown for you: startup hooks, workers and the command menu sync. If you drive the application some other way, call `await bot.startup()`, `await bot.stop()` and `await bot.shutdown()` yourself.

After `build()`, registration is closed: registering more handlers raises instead of being silently ignored.

## HTTP transports

`bot.build(request=..., get_updates_request=...)` replaces PTB's HTTP transports, for example with a proxy-aware client. Pass separate instances for the two: a long poll holds its connection, so sharing one would block every other API call. Once the bot is built its transports are fixed, and building again with different ones raises.

## Plain PTB handlers

`bot.raw(handler, group=0)` (or `router.raw(...)`) registers any PTB handler as is. Raw handlers in group 0 run after vitrine's own handlers; see [Handler precedence](https://github.com/target111/vitrine/blob/main/docs/handler-precedence.md).

```python
from telegram.ext import ChatMemberHandler

from vitrine import Bot

bot = Bot(token="...")


async def member_changed(update, context): ...


bot.raw(ChatMemberHandler(member_changed))
```

## Other ways through

- `Screen(extra={...})` passes keyword arguments to PTB's send and edit calls.
- Handlers can ask for `update`, `context` and `bot` directly and use PTB's API.
- `Bot(context_type=...)` sets a custom context class. Subclass `VitrineContext`, which allows vitrine to attach its own attributes to the context.
- `bot.application` is the built PTB `Application`.
