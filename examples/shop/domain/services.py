"""Domain services: in-memory stand-ins for a real database/payment gateway.

Async on purpose — swap the internals for SQL/Redis/RPC without touching the
bot layer. Still zero imports from telegram or vitrine.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Sequence

from .models import (
    InsufficientBalance,
    Order,
    OrderStatus,
    Product,
    UnknownProduct,
    User,
)


class UserService:
    def __init__(self, admin_tg_ids: Sequence[int] = ()) -> None:
        self._users: dict[int, User] = {}  # keyed by tg_id
        self._ids = itertools.count(1)
        self._admin_tg_ids = set(admin_tg_ids)

    async def get_or_create(self, tg_id: int, name: str) -> User:
        user = self._users.get(tg_id)
        if user is None:
            user = User(id=next(self._ids), tg_id=tg_id, name=name)
            if tg_id in self._admin_tg_ids:
                user.roles.add("admin")
            self._users[tg_id] = user

        return user

    async def by_tg_id(self, tg_id: int) -> User | None:
        return self._users.get(tg_id)

    async def count(self) -> int:
        return len(self._users)

    async def list_page(self, offset: int, limit: int) -> list[User]:
        users = sorted(self._users.values(), key=lambda u: u.id)
        return users[offset : offset + limit]

    async def set_banned(self, tg_id: int, banned: bool) -> User:
        user = await self.get_or_create(tg_id, "?")
        user.banned = banned

        return user

    async def grant(self, tg_id: int, role: str) -> User:
        user = await self.get_or_create(tg_id, "?")
        user.roles.add(role)

        return user


class CatalogService:
    def __init__(self) -> None:
        self._products = {
            p.sku: p
            for p in (
                Product("vpn30", "VPN — 30 days", 4.99, "Fast, no logs, 12 regions."),
                Product("vps1", "VPS — 1 vCPU", 6.00, "1 vCPU, 2 GB RAM, 20 GB NVMe."),
                Product(
                    "proxy10",
                    "Proxy pack ×10",
                    9.50,
                    "Ten rotating residential proxies.",
                ),
                Product(
                    "mail1",
                    "Mailbox — 1 year",
                    12.00,
                    "Private mailbox, custom domain.",
                ),
                Product("cdn1", "CDN — 100 GB", 3.25, "Edge caching, instant purge."),
            )
        }

    async def all(self) -> list[Product]:
        return list(self._products.values())

    async def get(self, sku: str) -> Product:
        product = self._products.get(sku)
        if product is None:
            raise UnknownProduct(sku)

        return product


class OrderService:
    def __init__(self, catalog: CatalogService) -> None:
        self._catalog = catalog
        self._orders: dict[int, Order] = {}
        self._ids = itertools.count(1000)

    async def place(self, user: User, chat_id: int, sku: str, qty: int) -> Order:
        product = await self._catalog.get(sku)
        total = round(product.price * qty, 2)
        if total > user.balance:
            raise InsufficientBalance(needed=total, available=user.balance)

        user.balance = round(user.balance - total, 2)
        order = Order(
            id=next(self._ids),
            user_id=user.id,
            chat_id=chat_id,
            sku=sku,
            qty=qty,
            total=total,
        )
        self._orders[order.id] = order

        return order

    async def for_user(self, user: User) -> list[Order]:
        return sorted(
            (o for o in self._orders.values() if o.user_id == user.id),
            key=lambda o: -o.created_at,
        )

    async def pending(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status is OrderStatus.PENDING]

    async def count(self) -> int:
        return len(self._orders)


class PaymentGateway:
    """Pretend chain watcher: each poll, some pending payments confirm."""

    async def poll_confirmations(self, orders: Sequence[Order]) -> list[Order]:
        confirmed = []
        for order in orders:
            if random.random() < 0.4:
                order.status = OrderStatus.PAID
                confirmed.append(order)

        return confirmed
