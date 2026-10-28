# Commands & /help

```python
from vitrine import Arg, Bot, Greedy, Screen

bot = Bot(token="...")


@bot.command("pay")
async def pay(wallet, amount: Arg[float], note: Greedy = ""):
    """Send money from your wallet.

    The note is optional and can contain spaces.
    """
    await wallet.pay(amount, note)
    return Screen(text=f"Paid {amount}.")
```

`/pay 5 thanks for lunch` calls `pay` with `amount=5.0` and `note="thanks for lunch"`. `wallet` is injected, see [Dependency injection](https://github.com/target111/vitrine/blob/main/docs/dependency-injection.md).

## Registering a command

`@bot.command(name, description=..., scope=..., hidden=..., edits=...)`:

- `name` defaults to the function name. Names Telegram would reject (uppercase, too long, other characters) fail when you register them.
- `description` defaults to the first paragraph of the docstring. The rest of the docstring is shown by `/help <command>`.
- `scope` groups the command in `/help` and decides which chats get it in their Telegram menu. See [Command menus](https://github.com/target111/vitrine/blob/main/docs/command-menus.md).
- `hidden=True` leaves the command out of `/help` and the menus. It still works when typed.
- `edits=True` also runs the command for edited messages. By default an edited `/command` is ignored, since editing an old message would otherwise run it again.

## Arguments

Arguments are declared explicitly; every other parameter is injected.

- `Arg[T]` is one space-separated word, converted by calling `T`. Type checkers see a plain `T`.
- `bool` accepts `yes`/`no`, `true`/`false`, `on`/`off`, `y`/`n` and `1`/`0`.
- `Arg[int | None]` and other unions try each type in turn.
- `Greedy` takes the rest of the line, spaces included, and must be the last argument.
- A parameter with a default is optional.

When the input doesn't fit, such as a missing argument, an extra one or a value that won't convert, the user gets the reason and a usage line like `/pay <amount> [note...]`, and the handler doesn't run.

Because nothing becomes an argument by accident, a misspelled service name is a build error instead of a surprise argument, and registering a new provider never changes a command's syntax.

An argument's type is called to convert the input, so it has to exist at runtime. With `from __future__ import annotations`, define it at module level, not inside a function, and don't import it only under `TYPE_CHECKING`. The build tells you if it can't resolve one.

## /help

vitrine registers `/help` unless you register your own or pass `Bot(help_command=False)`.

- `/help` lists the commands the caller can see, grouped by scope, with their descriptions.
- `/help <command>` shows one command in full: usage line, arguments, the rest of the docstring, and what governs it (scope, guards, rate limit).
- A command the caller can't see gets the same answer as one that doesn't exist.

Which scopes a caller sees is described in [Command menus](https://github.com/target111/vitrine/blob/main/docs/command-menus.md#who-sees-a-scope-in-help).

Conversation entry commands are listed and take arguments like any other command. Commands that only work inside a conversation, such as a state's `/skip` or `/cancel`, are left out. See [Conversations](https://github.com/target111/vitrine/blob/main/docs/conversations.md).
