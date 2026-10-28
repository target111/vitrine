# Workers & lifecycle

## Lifecycle hooks

```python
from vitrine import Bot

bot = Bot(token="...")


@bot.on_startup
async def warm_cache(catalog):
    await catalog.load()


@bot.on_shutdown
async def close_pool(db):
    await db.close()
```

Startup hooks run once the PTB application is initialized, before workers start and before the startup command menu sync. Shutdown hooks run when the bot shuts down. Both get dependency injection, with a fresh dependency scope per call.

## Workers

A worker is a background task tied to the bot's lifecycle. There are two kinds, registered with the same decorator:

```python
from vitrine import Screen


@bot.worker(every=30)  # runs again 30 seconds after each run finishes
async def reconcile(orders, delivery):
    for order in await orders.confirmed():
        await delivery.send(order.chat_id, Screen(text=f"Order {order.id} confirmed."))


@bot.worker()  # a long-running loop
async def watch_feed(feed, delivery):
    async for event in feed.stream():
        ...
```

- Workers get injection like handlers: providers, `bot` and `delivery`. There is no `update` or `context`, since a worker doesn't run for an update. Use `delivery.send(chat_id, screen)` to message users.
- They start after the startup hooks and are cancelled cleanly when the bot stops.
- A worker that raises is logged and restarted with exponential backoff, capped at `backoff_max` seconds (default 60).

`@bot.worker(every=..., name=..., initial_delay=..., backoff_max=...)`:

| Argument | Meaning |
|---|---|
| `every` | Seconds to wait after a run finishes before the next one. Without it, the function is a long-running loop, restarted if it crashes or returns. |
| `name` | Name used in logs; defaults to the function name |
| `initial_delay` | Seconds to wait before the first run |
| `backoff_max` | Upper limit for the restart delay after a crash |

In tests, `TestClient` doesn't start workers unless you pass `startup=True`, so periodic workers don't send messages in the middle of a test. See [Testing](https://github.com/target111/vitrine/blob/main/docs/testing.md).
