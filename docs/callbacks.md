# Callbacks

Inline-button data is a pydantic model with a short prefix. vitrine packs it into Telegram's `callback_data` string and validates it back into the model when the button is pressed.

```python
from vitrine import Bot, Button, CallbackData, Screen

bot = Bot(token="...")


class MenuCB(CallbackData, prefix="menu"):
    section: str
    page: int = 1


@bot.command("start")
async def start():
    return Screen(text="Menu", keyboard=[[Button("Shop", callback=MenuCB(section="shop"))]])


@bot.callback(MenuCB)
async def menu(data: MenuCB):  # the decoded payload is injected as `data`
    return Screen(text=f"{data.section}, page {data.page}")
```

Telegram limits `callback_data` to 64 bytes; packing a payload that is too long raises `CallbackDataError` at once instead of failing at send time.

## Stale and corrupt buttons

Buttons stay in old messages for as long as the chat exists. If a pressed button's data no longer matches its model, for example after a field was renamed, vitrine answers the press with "This button has expired." and logs a warning, instead of raising inside your handler.

## Wire formats

- **Positional** (the default): `menu:shop:2`. Compact, but the field order is part of the format.
- **Keyed** (`keyed=True`): `filter?category=books`. Fields equal to their default are left out and unknown keys are ignored, so buttons keep working when fields are added, reordered or removed.

```python
class FilterCB(CallbackData, prefix="filter", keyed=True):
    category: str
    page: int = 1
```

Each prefix belongs to one model; defining a second model with the same prefix raises `CallbackDataError`.

`keyed` only changes what new buttons contain. Decoding detects the format from the data itself, so you can switch a model to `keyed=True` while old positional buttons are still out there.

## Narrowing with `when=`

`when=` routes a subset of presses to a handler. A press it rejects stays available to the other handlers for the same model:

```python
class ItemCB(CallbackData, prefix="item"):
    kind: str


@bot.callback(ItemCB, when=lambda d: d.kind == "gift")
async def gift(data: ItemCB):
    return Screen(text="Wrapping it up.")


@bot.callback(ItemCB)
async def any_item(data: ItemCB):
    return Screen(text=f"A {data.kind}.")
```

A handler without `when=` takes every press of its model, so a second handler for the same model after it can never run. That is a build error; see [Handler precedence](https://github.com/target111/vitrine/blob/main/docs/handler-precedence.md).

## Unclaimed presses

A press that no handler takes is answered by a catch-all registered last in the default group, so the button doesn't spin until Telegram times it out.

Telegram accepts one answer per press, and PTB runs handler groups independently. A callback handler in a later group would only run after the catch-all has already answered, and its alert would never show. Keep callback handlers in the default group.

String callback data (`Button("Ping", callback="ping")`) still works for buttons you handle with a raw PTB handler.
