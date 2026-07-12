"""Admin tooling: scoped commands, guards reading the resolved principal."""

from __future__ import annotations

from vitrine import Paginator, Router, Screen, admin_only, throttle
from vitrine.logging import audit
from vitrine.markdown import Md, code

from domain.models import User
from domain.services import OrderService, UserService

from . import views
from .cbs import UsersPageCB

router = Router("admin")


class _UsersSource:
    def __init__(self, users: UserService) -> None:
        self._users = users

    async def count(self) -> int:
        return await self._users.count()

    async def fetch(self, offset: int, limit: int):
        return await self._users.list_page(offset, limit)


@router.command("users", description="List registered users", scope="admin")
@admin_only
async def users_cmd(user_service: UserService):
    page = await Paginator(_UsersSource(user_service), per_page=8).page(1)
    return views.users_page(page)


@router.callback(UsersPageCB)
@admin_only
async def users_page_cb(data: UsersPageCB, user_service: UserService):
    page = await Paginator(_UsersSource(user_service), per_page=8).page(data.page)
    return views.users_page(page)


@router.command("ban", description="Ban a user: /ban <tg_id>", scope="admin")
@admin_only
async def ban(user: User, user_service: UserService, tg_id: int):
    await user_service.set_banned(tg_id, True)
    audit("user.banned", actor=user.tg_id, target=tg_id)

    return Screen(text=Md().line("Banned ", code(tg_id), "."))


@router.command("unban", description="Lift a ban: /unban <tg_id>", scope="admin")
@admin_only
async def unban(user: User, user_service: UserService, tg_id: int):
    await user_service.set_banned(tg_id, False)
    audit("user.unbanned", actor=user.tg_id, target=tg_id)

    return Screen(text=Md().line("Unbanned ", code(tg_id), "."))


@router.command("grant", description="Grant a role: /grant <tg_id> [role]", scope="admin")
@admin_only
async def grant(user: User, user_service: UserService, tg_id: int, role: str = "support"):
    await user_service.grant(tg_id, role)
    audit("role.granted", actor=user.tg_id, target=tg_id, role=role)

    return Screen(text=Md().line("Granted ", code(role), " to ", code(tg_id), "."))


@router.command("stats", description="Quick totals", scope="admin")
@admin_only
@throttle(6, per=60)
async def stats(user_service: UserService, orders: OrderService):
    return Screen(
        text=Md()
        .heading("📈 Stats")
        .kv("Users", code(await user_service.count()))
        .kv("Orders", code(await orders.count()))
    )
