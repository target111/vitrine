# Dependency injection

Handlers are plain async functions. They declare what they need by parameter name, and vitrine supplies it.

```python
from vitrine import Bot, Screen

bot = Bot(token="...")

bot.provide_value("orders", OrderService())  # a constant or singleton


@bot.provide("session")  # a factory; may be async
async def session(db):  # factories can depend on other providers
    async with db.begin() as s:
        yield s  # the code after yield runs once the update is handled


@bot.command("orders")
async def list_orders(update, orders, session):
    count = await orders.count_for(update.effective_user.id, session)
    return Screen(text=f"You have {count} orders.")
```

## Where values come from

Every name a handler asks for is looked up in one namespace, in this order:

1. an explicit `Depends(factory)` default on the parameter;
2. values that belong to the current update:
   - `update`, `context`, `bot`, `delivery`
   - `data`: the decoded callback payload, for callback handlers
   - `state`: the conversation state, inside a conversation
   - `event`: what middleware sees, see [Middleware & errors](https://github.com/target111/vitrine/blob/main/docs/middleware-and-errors.md)
   - `error`: the exception, inside an error handler
   - the principal, under the name given to `Auth`, see [Auth](https://github.com/target111/vitrine/blob/main/docs/auth.md)
   - command arguments declared with `Arg[...]`
   - values a middleware put in `event.extras`; register a provider under the same name too, so the build-time check knows the name
3. registered providers.

A parameter with a default is optional: if nothing supplies it, the default is used.

## Providers

```python
import time

bot.provide_value("config", settings)  # a value


@bot.provide("db")  # a named factory
def make_db(config):
    return Database(config.db_url)


@bot.provide  # named after the function: "clock"
def clock():
    return time.monotonic
```

A factory can be sync or async, and it can be a generator or async generator: the part after `yield` runs as cleanup, like FastAPI dependencies. Factories declare their own parameters, which are resolved the same way.

`Depends(fn)` uses a factory for one parameter without registering it:

```python
from vitrine import Depends


@bot.command("token")
async def show_token(token: str = Depends(make_token)):
    return Screen(text=token)
```

## One scope per update

Resolved providers are cached for the whole update. The ban gate, every handler group, conversation hooks and error handlers share one scope, and generator cleanups run after the update has been fully processed. So if your auth resolver loads the user with `session`, the handler gets that same user and the same session, which is still open.

Workers and lifecycle hooks get a fresh scope per call.

## Checked at startup

`bot.build()` (and so `bot.run()`) checks every handler's parameters. A name that no provider, framework value or argument can supply is a build error that names the handler and the parameter, instead of a failure the first time a user triggers it.

Annotations on injected parameters are documentation, not a contract. Duck-typed stand-ins are fine, and test suites rely on that. But an annotation that provably disagrees with its provider, such as `count: int` against a provider annotated to return `str`, would otherwise show up later as an `AttributeError` deep inside a handler. The build reports these on the `vitrine.build` logger. Only what a subclass check can settle is reported: protocols, generics, unions, unannotated factories and names imported only under `TYPE_CHECKING` are left alone, and an `int` where a `float` is expected is fine.

`Bot(strict_types=True)` turns these warnings into build failures.
