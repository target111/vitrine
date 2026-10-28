# Handler precedence

PTB runs at most one handler per handler group for each update: the first one that matches. vitrine registers handlers in group 0 in a fixed order, so you can predict which one runs.

## Order in group 0

1. **Reply-keyboard buttons** (`@bot.reply_button`). They are navigation, so they work from anywhere, even in the middle of a conversation.
2. **Conversations.** First the user's live run, if any: its current state's steps, then its `/cancel`. Then every conversation's entry points. A catch-all `@bot.message()` can't swallow the answer a step is waiting for, and a conversation's `/cancel` wins over a bot-wide `/cancel` only while a run is live. See [Conversations](https://github.com/target111/vitrine/blob/main/docs/conversations.md).
3. **Every other handler**, in registration order, then raw PTB handlers registered with `bot.raw()`.
4. **A catch-all that answers button presses** nothing else claimed, so buttons don't spin until they time out.

Handlers registered with `group=` in another group run independently, as in PTB. Keep callback handlers in group 0; see [Callbacks](https://github.com/target111/vitrine/blob/main/docs/callbacks.md#unclaimed-presses).

## Routers

A `Router` collects handlers and can be split across modules and packages, then mounted on the bot:

```python
from vitrine import Bot, Router, Screen

admin = Router("admin")


@admin.middleware
async def audit_admin(event, call_next):
    return await call_next(event)


@admin.command("stats", scope="admin")
async def stats():
    return Screen(text="...")


bot = Bot(token="...")
bot.include(admin)
```

Routers nest with `router.include(child)`. A router's middleware applies to its own handlers and conversations and to those of its children. Bot middleware runs outermost, then each router's from the root down. For precedence, a router's own handlers come before its children's, and children come in the order they were included.

## Edited messages

Telegram sends an edited message as a new update containing the whole message, so a handler that ran on the original would run again. Commands, message handlers and conversation steps ignore edits by default; pass `edits=True` to opt a handler in.

## Checked at build time

Handlers that could never run fail the build:

- the same command registered twice in one group, including conversation entry commands;
- a callback handler behind another handler that takes every press of the same model, including a conversation entry. Narrow the earlier one with `when=`.

Registering anything after `bot.build()` (or `bot.run()`) raises instead of being silently ignored.
