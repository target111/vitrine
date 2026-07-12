"""Identity: a first-class, app-defined principal + guard decorators.

The app owns the principal type (any object — a dataclass, an ORM row); the
framework owns *resolve once per update, cache, inject, guard*::

    @dataclass
    class User:
        id: int
        roles: set[str]
        banned: bool

    async def resolve_user(update, user_service) -> User | None:
        tg = update.effective_user
        return await user_service.get_or_create(tg.id, tg.full_name) if tg else None

    bot = Bot(token, auth=Auth(resolve_user, name="user",
                               roles=lambda u: u.roles,
                               is_banned=lambda u: u.banned))

Any handler that declares a ``user`` parameter receives the resolved principal;
guards (:func:`requires`, :func:`admin_only`) read from the same object; bans
are enforced bot-wide before any handler runs.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Generic, Iterable, TypeVar

from .exceptions import BannedError, NotAuthorizedError
from .injection import Invocation, Providers, resolve_kwargs

P = TypeVar("P")

_ROLES_ATTR = "__vitrine_required_roles__"
_ADMIN_ATTR = "__vitrine_admin_only__"

_CACHE_ATTR = "__vitrine_principal__"


class Auth(Generic[P]):
    """Configuration of identity resolution and permission accessors."""

    def __init__(
        self,
        resolver: Callable[..., Awaitable[P | None]],
        *,
        name: str = "principal",
        roles: Callable[[P], Iterable[str]] | None = None,
        is_banned: Callable[[P], bool] | None = None,
        is_admin: Callable[[P], bool] | None = None,
    ) -> None:
        self.resolver = resolver
        self.name = name
        self._roles = roles
        self._is_banned = is_banned
        self._is_admin = is_admin

    async def resolve(self, inv: Invocation, providers: Providers) -> P | None:
        """Resolve the caller once per update; later calls hit the cache."""
        context = inv.context
        if context is not None:
            cached = getattr(context, _CACHE_ATTR, None)
            if cached is not None:
                update_id, principal = cached
                if update_id == id(inv.update):
                    return principal

        kwargs = await resolve_kwargs(self.resolver, inv, providers)
        principal = await self.resolver(**kwargs)

        if context is not None:
            try:
                setattr(context, _CACHE_ATTR, (id(inv.update), principal))
            except AttributeError:
                pass  # exotic context type without attribute support

        return principal

    def roles_of(self, principal: P | None) -> set[str]:
        if principal is None or self._roles is None:
            return set()
        return set(self._roles(principal))

    def banned(self, principal: P | None) -> bool:
        if principal is None or self._is_banned is None:
            return False
        return bool(self._is_banned(principal))

    def admin(self, principal: P | None) -> bool:
        if principal is None:
            return False

        if self._is_admin is not None:
            return bool(self._is_admin(principal))

        return "admin" in self.roles_of(principal)

    def check(self, fn: Callable[..., Any], principal: P | None) -> None:
        """Enforce the guard markers on ``fn`` against a resolved principal."""
        if self.banned(principal):
            raise BannedError()

        if getattr(fn, _ADMIN_ATTR, False) and not self.admin(principal):
            raise NotAuthorizedError(missing_roles=("admin",))

        required: tuple[str, ...] = getattr(fn, _ROLES_ATTR, ())
        if required:
            missing = set(required) - self.roles_of(principal)
            if missing and not self.admin(principal):
                raise NotAuthorizedError(missing_roles=sorted(missing))


def requires(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Guard: the resolved principal must hold every listed role (admins pass)."""

    def mark(fn: Callable[..., Any]) -> Callable[..., Any]:
        current: tuple[str, ...] = getattr(fn, _ROLES_ATTR, ())
        setattr(fn, _ROLES_ATTR, (*current, *roles))
        return fn

    return mark


def admin_only(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Guard: only principals for whom ``Auth.is_admin`` is true."""
    setattr(fn, _ADMIN_ATTR, True)
    return fn


def has_guards(fn: Callable[..., Any]) -> bool:
    return bool(getattr(fn, _ROLES_ATTR, ())) or bool(getattr(fn, _ADMIN_ATTR, False))
