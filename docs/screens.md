# Screens & delivery

A `Screen` is a message as a value: text, an inline keyboard, optional media and send options. Building one needs no `Update` and does no I/O, so views can be plain functions you test by calling them.

```python
from vitrine import Button, CallbackData, Photo, Screen


class ProductCB(CallbackData, prefix="product"):
    sku: str


def product_view(product) -> Screen:
    return Screen(
        text=f"{product.name}: {product.price}",
        media=Photo(product.image_url),
        keyboard=[[Button("Buy", callback=ProductCB(sku=product.sku))]],
    )
```

## Rendering

Return a `Screen` from a handler and vitrine renders it: a button press edits the message the button was on, and anything else gets a reply. You can also deliver screens yourself through `delivery`, which every handler can ask for:

| Call | Does |
|---|---|
| `delivery.render(update, screen)` | What a returned screen does: edit for button presses, reply otherwise. `fresh=True` always sends a new message. |
| `delivery.send(chat_id, screen)` | Sends to any chat, for workers and notifications |
| `delivery.edit(message, screen)` | Edits a given message into the screen |
| `screen.render(update, context)` | Shortcut for `delivery.render` |

## What delivery handles

- **Text and media switches.** Telegram can't edit a text message into a photo, or between some media kinds. vitrine sends the new message first and then deletes the old one, so a failure never leaves the user with nothing.
- **Media kinds.** Photo, video, animation, document, audio, voice, video note and sticker are all detected on existing messages. Build media with `Photo(...)`, `Video(...)`, `Animation(...)`, `Document(...)`, `Audio(...)` or `Voice(...)` from bytes, a `Path` or a URL, or pass a `file_id`.
- **Upload caching.** Uploads are cached by content hash and re-sent by `file_id`. If Telegram rejects a cached `file_id`, the file is uploaded once more and the cache is updated. The default cache lives in memory; pass `Bot(file_ids=...)` with your own `FileIdCache` (`get`/`set`) to keep it in Redis or a database.
- **Unchanged edits.** Telegram's "message is not modified" error is ignored.

## Screen fields

| Field | Meaning |
|---|---|
| `text` | A string, or a node from the [Markdown builder](https://github.com/target111/vitrine/blob/main/docs/utilities.md#markdown-builder), which is escaped for you |
| `keyboard` | Rows of `Button`, or a PTB `InlineKeyboardMarkup` |
| `media` | A `Media` value, built with `Photo(...)` and friends |
| `reply_keyboard` | A `ReplyKeyboard`, see below |
| `parse_mode` | For plain-string text that already contains markup |
| `link_preview` | `False` by default |
| `extra` | Extra keyword arguments for PTB's send and edit calls |

`Button(text, callback=..., url=..., style=...)` takes either typed callback data, see [Callbacks](https://github.com/target111/vitrine/blob/main/docs/callbacks.md), or a URL. `style` colors the button on clients that support it: `"primary"`, `"success"` or `"danger"`.

`screen.content()` and `screen.markup()` show what would be sent, which is handy in tests.

## Reply keyboards

A reply keyboard sits under the input field instead of under a message. It works well as a launcher that is always there: set it once, and each press routes to its handler from anywhere in the bot.

```python
from vitrine import Bot, ReplyKeyboard, Screen

bot = Bot(token="...")

LAUNCHER = ReplyKeyboard([["🛍 Shop", "ℹ️ Help"]])  # persistent and resized by default


@bot.command("start")
async def start():
    return Screen(text="Welcome!", reply_keyboard=LAUNCHER)


@bot.reply_button("🛍 Shop")
async def shop():
    return Screen(text="The shop")
```

- Reply-button handlers run before conversations and other handlers, so the launcher works even in the middle of a conversation. See [Handler precedence](https://github.com/target111/vitrine/blob/main/docs/handler-precedence.md).
- A message carries an inline keyboard or a reply keyboard, not both, and `Screen` refuses to hold both.
- Telegram can't attach a reply keyboard when editing a message, so editing into a screen that has one sends a new message and deletes the old one.
- `Screen(reply_keyboard=REMOVE_REPLY_KEYBOARD)` takes the keyboard away.
- `ReplyButton(text, request_contact=True)` and `request_location=True` ask the client to share those instead of sending text. Handle the result with `@bot.message(filters.CONTACT)` or `filters.LOCATION`; it never reaches a `@bot.reply_button` handler.

[`examples/launcher_bot.py`](https://github.com/target111/vitrine/blob/main/examples/launcher_bot.py) is a complete launcher bot.
