# Utilities

## Rate limiting

```python
from vitrine import Bot, Screen, throttle

bot = Bot(token="...")


@bot.command("roll")
@throttle(3, per=60)  # 3 calls per user per minute
async def roll():
    return Screen(text="🎲")
```

The limit is a sliding window. By default each user gets their own count per handler. `key=` changes that: it takes the middleware `event` and returns a string, so you can limit per chat or share one count between handlers. Returning `None` skips the limit for that update.

Over the limit, the handler doesn't run and the user is told to slow down (`RateLimitedError`, see [Middleware & errors](https://github.com/target111/vitrine/blob/main/docs/middleware-and-errors.md)). `on_limit=` replaces that: an async function taking `(event, retry_after)`. It can do nothing at all, which drops the update silently.

```python
async def ignore(event, retry_after):
    pass


@bot.callback(VoteCB)
@throttle(1, per=5, key=lambda e: f"vote:{e.update.effective_chat.id}", on_limit=ignore)
async def vote(data: VoteCB): ...
```

## Pagination

Pagination needs a source with two async methods, `count()` and `fetch(offset, limit)`, so a database-backed source only loads the page it shows. `ListSource` wraps a list you already have.

```python
from vitrine import CallbackData, ListSource, Paginator, Screen, nav_row


class OrdersCB(CallbackData, prefix="orders"):
    page: int = 1


@bot.callback(OrdersCB)
async def orders_page(data: OrdersCB, orders):
    page = await Paginator(ListSource(await orders.all()), per_page=5).page(data.page)
    return Screen(
        text="\n".join(str(order) for order in page.items) or "No orders yet.",
        keyboard=[nav_row(page, lambda n: OrdersCB(page=n))],
    )
```

`page` has `items`, `number` (1-based), `per_page` and `total`. `nav_row` builds Previous, page counter and Next buttons; at the first and last page the missing direction becomes a blank button that does nothing. `labels=` and `counter=False` change the buttons.

## Markdown builder

`vitrine.markdown` builds formatted text as a tree of nodes, so styles nest and user input is always escaped. Put the result in `Screen(text=...)` and vitrine sets the parse mode for you.

```python
from vitrine.markdown import Md, bold, code, italic, link

text = (
    Md()
    .heading("Order ", link("#1042", "https://shop.example/o/1042"))
    .kv("Total", code("12.50 EUR"))
    .bullet("Paid by ", bold(customer_name))  # escaped, whatever the customer typed
    .line(italic("Thanks for shopping with us."))
)
screen = Screen(text=text)
```

- Inline nodes: `text`, `bold`, `italic`, `underline`, `strikethrough`, `spoiler`, `code`, `link(label, url)` and `mention(label, user_id)`. Plain strings are text nodes and are escaped.
- Blocks: `pre(text, language=None)` and `blockquote(text, expandable=False)`.
- `Md()` methods: `line`, `blank`, `heading`, `bullet`, `kv`.
- `raw("...")` passes pre-rendered markup through unescaped. Use it with care.

Both MarkdownV2 (the default) and the old Markdown are supported; `Bot(markdown_version=1)` switches. `escape(text, version)` escapes a string on its own.

## Logging

vitrine logs one line per handled update on the `vitrine.update` logger, with the handler, user, chat, duration and outcome. For your own events:

```python
from vitrine import audit, log_event, setup_logging

setup_logging()  # optional: key=value lines on stderr

audit("order.placed", actor=user_id, order=order_id, total=total)  # on the vitrine.audit logger
log_event(logger, "cache.miss", key=key)  # on any logger
```

`setup_logging(level)` installs a key=value formatter on the root logger. It is entirely optional; vitrine's loggers work with any logging setup.

## Downloads

`download` saves a file the user sent to a temporary path and deletes it when the block ends:

```python
from telegram.ext import filters

from vitrine import download


@bot.message(filters.Document.ALL)
async def got_document(update, bot):
    async with download(bot, update.effective_message.document.file_id) as path:
        size = path.stat().st_size
    return Screen(text=f"Received {size} bytes.")
```

It takes `timeout=` (default 60 seconds), `directory=` and `suffix=`.

Uploads go the other way through `Screen(media=...)`, which caches `file_id`s; see [Screens & delivery](https://github.com/target111/vitrine/blob/main/docs/screens.md).
