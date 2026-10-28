# Middleware & errors

## Middleware

A middleware is `async def mw(event, call_next)`. It wraps every update vitrine handles: commands, callbacks, messages, reply buttons, conversation steps and exit hooks.

```python
import time

from vitrine import Bot

bot = Bot(token="...")


@bot.middleware
async def timing(event, call_next):
    started = time.monotonic()
    try:
        return await call_next(event)
    finally:
        metrics.observe(event.handler_name, time.monotonic() - started)
```

Register it on the bot or on a `Router` (`@router.middleware`). Bot middleware runs outermost, then each router's from the root down; see [Handler precedence](https://github.com/target111/vitrine/blob/main/docs/handler-precedence.md#routers).

`event` has `update`, `context`, `handler_name`, `data` (decoded callback data, if any), `state` (the conversation state, if any) and `extras`.

Anything a middleware puts in `event.extras` can be injected into the handler by name, and takes precedence over a provider of the same name. That is how, for example, an i18n middleware hands each user's translator to every handler:

```python
from vitrine import Screen

bot.provide_value("t", translator_for(None))  # the default, and what the build checks against


@bot.middleware
async def i18n(event, call_next):
    user = event.update.effective_user
    event.extras["t"] = translator_for(user.language_code if user else None)
    return await call_next(event)


@bot.command("hello")
async def hello(t):
    return Screen(text=t("hello"))
```

The provider matters: handler parameters are checked when the bot is built, before any middleware has run, so a name only a middleware supplies would fail the build.

A middleware that returns without calling `call_next` stops the handler from running. Inside a conversation, an entry stopped that way doesn't start a run.

Middleware doesn't wrap the ban gate, workers or lifecycle hooks.

## Errors

When a handler raises, vitrine looks up an error handler for the exception type, walking the class hierarchy so the most specific registered type wins:

```python
from vitrine import Button, Screen


class InsufficientFunds(Exception):
    def __init__(self, needed: float):
        self.needed = needed


@bot.on_error(InsufficientFunds)
async def insufficient(error: InsufficientFunds):
    return Screen(
        text=f"Not enough balance: you need {error.needed}.",
        keyboard=[[Button("Back", callback=MenuCB(section="wallet"))]],
    )
```

An error handler gets injection like a handler, with the exception as `error`. If it returns a `Screen`, the screen is rendered into the current message: edited in place for a button press, sent as a reply otherwise. It can also return `None` after doing its own thing. If an error handler itself fails, that is logged and the user gets the generic message.

### Built-in handling

These defaults are registered for you, and `@bot.on_error` replaces any of them:

| Exception | What the user sees |
|---|---|
| `UserFacingError` and subclasses | Its `message`: an answer to a button press (an alert if `show_alert=True`), otherwise a reply |
| `UsageError` | The reason and the command's usage line |
| `NotRegisteredError`, `NotAuthorizedError`, `BannedError` | A short alert explaining the refusal |
| `RateLimitedError` | A request to slow down, with how many seconds to wait |
| `TelegramError` | "Telegram hiccuped. Please try again." (logged as a warning) |
| anything else | "Something went wrong. Please try again." (logged with the traceback) |

Raise `UserFacingError("...")` from your own code to show a message without writing a handler for it. Its subclasses `AuthError`, `NotRegisteredError`, `NotAuthorizedError` and `BannedError` cover auth, see [Auth](https://github.com/target111/vitrine/blob/main/docs/auth.md).

Configuration mistakes raise `ConfigurationError` at build time. All vitrine exceptions derive from `FrameworkError`.
