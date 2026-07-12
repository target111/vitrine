"""Identity wiring: Telegram caller -> domain User, once per update."""

from __future__ import annotations

from vitrine import Auth

from domain.models import User
from domain.services import UserService


async def resolve_user(update, user_service: UserService) -> User | None:
    """``user_service`` is injected — the resolver is a DI target like any handler."""
    tg_user = getattr(update, "effective_user", None)
    if tg_user is None:
        return None

    return await user_service.get_or_create(tg_user.id, tg_user.first_name)


def make_auth() -> Auth[User]:
    return Auth(
        resolve_user,
        name="user",  # every handler that declares `user` gets the domain User
        roles=lambda u: u.roles,
        is_banned=lambda u: u.banned,
        is_admin=lambda u: u.is_admin,
    )
