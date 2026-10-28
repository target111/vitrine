# Auth

You define the principal type: a dataclass, an ORM row, anything. vitrine resolves it once per update, injects it into handlers, enforces bans and checks guards.

```python
from dataclasses import dataclass

from vitrine import Arg, Auth, Bot, Screen, admin_only, requires, requires_principal


@dataclass
class User:
    id: int
    roles: set[str]
    banned: bool = False


async def load_user(update, users) -> User | None:
    tg = update.effective_user
    return await users.get(tg.id) if tg else None


auth = Auth(
    load_user,
    name="user",  # handlers ask for the principal as `user`
    roles=lambda u: u.roles,
    is_banned=lambda u: u.banned,
)
bot = Bot(token="...", auth=auth)


@bot.command("refund")
@requires("support")  # every listed role is required; admins always pass
async def refund(user: User, order_id: Arg[int]):
    return Screen(text=f"Refunded order {order_id}.")


@bot.command("ban")
@admin_only
async def ban(user_id: Arg[int]): ...


@bot.command("profile")
@requires_principal  # the caller has to be known, e.g. registered via /start
async def profile(user: User):
    return Screen(text=f"Your id is {user.id}.")
```

## Resolving the principal

The resolver is injected like a handler, so it can ask for `update` and any provider (`users` above). It returns the principal, or `None` when the caller is unknown.

It runs at most once per update, in the update's dependency scope. Every handler that asks for the principal gets the same object, and providers the resolver used, such as a database session, stay open until the update is done. See [Dependency injection](https://github.com/target111/vitrine/blob/main/docs/dependency-injection.md).

## Options

| `Auth(...)` argument | Meaning |
|---|---|
| `resolver` | Loads the principal for an update, or returns `None` |
| `name` | The parameter name handlers use for it (default `"principal"`) |
| `roles` | `principal -> iterable of role names`, for `@requires` |
| `is_banned` | `principal -> bool`, enforced on every update |
| `is_admin` | `principal -> bool`; without it, a principal with the `"admin"` role is an admin |

## Guards

- `@requires("a", "b")`: the principal must hold every listed role. Admins pass.
- `@admin_only`: the principal must be an admin.
- `@requires_principal`: a principal must resolve at all.

Any guard fails with `NotRegisteredError` when no principal resolves, so a guarded command tells unknown users to register first. A principal without the required roles gets `NotAuthorizedError`. Both are shown to the user as short messages (an alert for button presses). You can replace those messages with your own error handlers, see [Middleware & errors](https://github.com/target111/vitrine/blob/main/docs/middleware-and-errors.md).

Guards are not the same as command scopes. A scope decides who sees a command in `/help` and the Telegram menu; a guard decides who can run it. See [Command menus](https://github.com/target111/vitrine/blob/main/docs/command-menus.md).

## Bans

With `is_banned` set, bans are checked bot-wide before any handler in any group runs. A banned user's updates stop there; a banned user pressing a button gets an alert saying they are banned. If the resolver itself fails during that check, the error is logged and the update continues to the handlers.
