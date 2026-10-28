# Conversations

A conversation is a multi-step flow, such as an order form: the bot asks something, the user answers, and the answer decides the next step. Each run keeps a state object, usually a dataclass, and moves between named states.

```python
from dataclasses import dataclass

from vitrine import ANY_STATE, CANCEL, END, Arg, Bot, Conversation, ExitReason, Screen

bot = Bot(token="...")


@dataclass
class OrderState:
    sku: str = ""
    qty: int = 0


order = Conversation("order", OrderState, timeout=300)


@order.entry(command="order", description="Order a product")
async def begin(state: OrderState, sku: Arg[str]):
    state.sku = sku
    return "qty", Screen(text="How many?")


@order.state("qty")
async def got_qty(state: OrderState, update):
    text = update.effective_message.text
    if not text.isdigit():
        return None, Screen(text="Please send a number.")  # stay in "qty"
    state.qty = int(text)
    return END, Screen(text=f"Ordered {state.qty} x {state.sku}.")


@order.cancel()
async def cancel():
    return Screen(text="Cancelled.")


@order.on_exit
async def on_exit(state: OrderState, reason: ExitReason):
    if reason is not ExitReason.FINISHED:
        ...  # release a stock hold, log the abandoned order


bot.conversation(order)
```

## Steps

- `@conv.entry(...)` starts a run. It takes `command=`, `callback=` (a `CallbackData` model) or `filters=` (PTB message filters). A command entry is a normal command: it is listed in `/help`, published to the menu, and takes `Arg[...]` arguments, so `/order ABC` can start the flow with what it needs. A deep link `t.me/yourbot?start=ABC` arrives as `/start ABC`, so an entry on `command="start"` with an argument receives its payload.
- `@conv.state("name", ...)` handles updates while the run is in that state. By default it takes text messages that aren't commands; `callback=`, `command=` and `filters=` change that. One step can be mounted on several states, or on every state with `ANY_STATE`.
- `@conv.cancel(command="cancel")` is a command valid in every state that ends the run as cancelled. It may return a `Screen` to show.
- `@conv.on_exit` runs whenever a run ends, with the reason.

Steps are ordinary handlers. They get injection, middleware, guards, rate limits and error handling like any other handler, and the `state` parameter is the run's state object.

A command attached to a state, such as `@conv.state("qty", command="skip")`, only works in that state, so it stays out of `/help`. `when=` narrows a callback step to some presses without taking the others.

## What a step returns

| Return value | Effect |
|---|---|
| a state name, e.g. `"qty"` | move to that state |
| `None` | stay in the current state |
| `END` | finish the run; the exit hook gets `FINISHED` |
| `CANCEL` | cancel the run; the exit hook gets `CANCELLED` |
| `(any of the above, Screen(...))` | the same, and show the screen |
| `Screen(...)` alone | show the screen and stay |

Returning a state the conversation doesn't declare is a bug in the bot: the user gets the generic error message and the run stays where it was.

A Cancel button that works in every state is one step on `ANY_STATE` that returns `CANCEL`:

```python
@order.state(ANY_STATE, callback=CancelCB)
async def cancel_button():
    return CANCEL, Screen(text="Cancelled.")
```

## Exits

The exit hook gets the state and an `ExitReason`:

- `FINISHED`: a step returned `END`;
- `CANCELLED`: a step returned `CANCEL`, the user sent `/cancel`, or another conversation replaced this one (see below);
- `TIMEOUT`: the run was inactive for longer than `timeout` seconds.

The hook can return a `Screen`, for example a goodbye on timeout. When the run was replaced by another conversation, that screen is dropped, because the new conversation's first screen is already the answer to the update.

The timeout measures inactivity. The clock restarts after every handled step, and a slow step never times out while it is still running. Runs live in memory, so they don't survive a restart. Stopping the bot cancels pending timeouts and any timeout exit hook still running.

## One run per user per chat

A run belongs to one user in one chat, and each user has at most one live run per chat.

While a run is live, its current state's steps and its `/cancel` see that user's updates first, before any conversation's entry points and before the bot's other handlers. This holds whatever order conversations were registered in, so a button the run is waiting for is never taken by another conversation's entry.

An update the run doesn't take can still start a conversation: a different one, or the same one again from the top. The new run replaces the old one once its first screen has gone out, and the old run's exit hook gets `CANCELLED`.

An entry that doesn't start a run leaves everything as it was, including the user's current run. That covers bad arguments (the user gets the usage line), a failed guard, a rate limit, a middleware that answered without calling the handler, and an entry that returns a bare `Screen`.

Two cases to keep in mind:

- An entry that matches plain messages (`filters=`) can restart the flow whenever the current state doesn't take a message. Narrow such entries.
- A state whose filters accept commands takes them as answers, so an entry command can't restart the flow from that state.

## Inspecting a run

```python
order.active(chat_id=chat_id, user_id=user_id)  # ("qty", OrderState(...)) or None
```

It returns the user's live run in that conversation as `(state_name, state)`, or `None`.

## Example

[`examples/shop/botapp/order_flow.py`](https://github.com/target111/vitrine/blob/main/examples/shop/botapp/order_flow.py) puts these together: two entry points (a Buy button and `/order <sku>`), a `/skip` command in one state, a Cancel button on every state, and an exit hook that records abandoned orders.
