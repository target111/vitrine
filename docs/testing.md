# Testing

## Views

A `Screen` is plain data, so view functions are tested by calling them. `screen.content()` returns the text and parse mode that would be sent, and `screen.markup()` the inline keyboard.

```python
def test_product_view():
    screen = product_view(Product(sku="ABC", name="Mug", price="9 EUR"))
    assert screen.content() == ("Mug: 9 EUR", None)
```

## The test client

For everything else, `vitrine.testing.TestClient` runs your bot through PTB's real dispatcher, with Telegram faked at the HTTP layer. Handler groups, filters, the ban gate, conversations and error handlers all behave as in production, and every Bot API call the bot makes is recorded instead of sent.

```python
from vitrine.testing import TestClient, sent_texts


async def test_ordering():
    async with TestClient(build_bot()) as client:
        await client.send("/order ABC")
        assert sent_texts(await client.send("2")) == ["Ordered 2 x ABC."]
```

Async tests need an async-capable runner; vitrine's own suite uses `pytest` with `pytest-asyncio` in `asyncio_mode = "auto"`.

### Driving updates

Each of these processes one update and returns the API calls the bot made while handling it:

| Method | Simulates |
|---|---|
| `client.send(text)` | the user sending a message, or a command if it starts with `/` |
| `client.press(data)` | the user pressing an inline button with this callback data, on the bot's latest message unless `message_id=` names another |
| `client.edit(message_id, text)` | the user editing one of their earlier messages |
| `client.feed(update)` | any update, as a PTB `Update` or its Bot API dict |

`send`, `press` and `edit` take `user_id=` and `chat_id=` to act as another user or in another chat. By default the user is `42`, in their private chat. `TestClient(bot, user_id=..., chat_id=...)` changes the defaults.

### Checking what the bot did

- `sent_texts(calls)` extracts the texts from a list of calls, such as what one `send` returned.
- `client.calls("editMessageText")` returns every recorded call to one API method. Each call has `method`, `params` and `text`.
- `client.texts()` and `client.last_text()` cover every text and caption the bot sent or edited in so far.
- `client.requests` is the full list of calls.

### Making Telegram fail

`client.telegram` is the fake Telegram. `client.telegram.fail(method, description, retry_after=..., times=...)` makes the next calls to a method fail, for example to test flood control (`retry_after`) or a rejected edit.

### Startup

By default the client doesn't run startup hooks, workers or the command menu sync, so periodic workers don't send messages in the middle of a test. `TestClient(bot, startup=True)` runs them.

### One bot per client

A bot is built once, against one client's fake Telegram. Give each `TestClient` a fresh bot from a factory, such as `build_bot()` above; passing a bot that was already built for another client raises `ConfigurationError`.

```python
from vitrine import Bot


def build_bot() -> Bot:
    bot = Bot(token="123:TEST")
    bot.conversation(order)
    return bot
```

Module-level state such as a `Conversation` object can be shared: building a new bot rebinds it. Its handlers must be registered before the first build, though, since a built conversation is frozen.

vitrine's own test suite is written this way, including [`tests/test_examples.py`](https://github.com/target111/vitrine/blob/main/tests/test_examples.py), which places an order in the shop example.
