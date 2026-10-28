# Command menus

Telegram shows a command menu next to the input field. vitrine publishes it from your registered commands, and can give different chats different menus.

A command's `scope` is two things: a heading it is grouped under in `/help`, and the audience whose menu includes it. `scope_chats` says which chats belong to each scope:

```python
from vitrine import Arg, Bot

bot = Bot(
    token="...",
    scope_chats={
        "admin": get_admin_chat_ids,  # a list, or a function (sync or async) returning one
        "vip": get_vip_chat_ids,
    },
)


@bot.command("ban", scope="admin")
async def ban(user_id: Arg[int]): ...
```

A scope is not access control: a user outside the `admin` scope can still type `/ban`. Guard the handler for that, see [Auth](https://github.com/target111/vitrine/blob/main/docs/auth.md).

## How menus are kept in sync

`setMyCommands` writes to Telegram and the menu stays there until something replaces or deletes it. So vitrine reconciles Telegram's state with what it should be, instead of just writing:

- **One menu per chat.** Telegram stores a single chat-scoped menu per chat, so each chat gets the default commands plus every scope that names it, merged. A chat in both `admin` and `vip` sees both sets.
- **Leaving clears the menu.** A chat that drops out of every scope has its menu deleted, which drops it back to the default menu. A demoted admin doesn't keep the admin menu.
- **Unchanged chats are skipped.** vitrine remembers what each chat was last given and only writes chats whose menu would change.
- **Startup doesn't block.** The startup sync runs in the background, so the bot answers updates at once however many chats it has to write. Telegram's flood control (`RetryAfter`) is waited out, not skipped.

After a promotion or demotion, resync instead of restarting:

```python
await bot.sync_commands()  # every chat
await bot.sync_commands(chats=[chat_id])  # only these chats; nothing else is written or cleared
```

## Remembering menus across restarts

By default vitrine remembers published menus in memory, so after a restart every scoped chat is written once more. That is harmless, except for one case: a chat that left every scope while the bot was down still holds its old menu, and the new process doesn't know to clear it.

There are two ways to handle that:

- **`menu_store=`**: a `MenuStore` keeps a fingerprint of each chat's menu somewhere persistent, such as Redis or a table. It has three async methods: `load()` returning `{chat_id: fingerprint}`, `set(chat_id, fingerprint)` and `discard(chat_id)`. With it, a restart skips unchanged chats and still clears chats that left while the bot was down. If a store call fails, that chat is written or cleared again on the next sync.
- **`known_scope_chats=`**: without a persistent store, name the chats a previous process may have published to, for example every chat any scope has ever included. It takes a list or a function, like `scope_chats`. The first full sync that completes clears the ones that no longer belong to any scope. Clearing a chat that has no menu does nothing, so a generous list is safe.

## Who sees a scope in /help

With `scope_chats`, `/help` in a chat shows exactly the scopes that chat's menu carries. This calls the `scope_chats` functions, so keep them reasonably fast.

Without `scope_chats`, scopes only group `/help` and no per-chat menus are published. Then `scope_member=lambda scope, principal: ...` decides who sees each scope, and without it, admins see every scope (see [Auth](https://github.com/target111/vitrine/blob/main/docs/auth.md)). Passing both `scope_chats` and `scope_member` is an error.

## Checked at build time

- a `scope` that `scope_chats` doesn't list, which is almost always a typo;
- `"default"` listed in `scope_chats` (the default commands are already part of every menu);
- a command name Telegram would reject. Telegram refuses the whole batch for one bad name, which would leave every menu stuck at whatever was last written.
