"""Customer-facing menus: thin handlers that call services and return views."""

from __future__ import annotations

from vitrine import Paginator, Router

from domain.models import User
from domain.services import CatalogService, OrderService

from . import views
from .cbs import MenuCB, OrdersPageCB, ProductCB

router = Router("menus")


@router.command("start", description="Open the shop")
async def start(user: User):
    return views.home(user)


@router.callback(MenuCB, when=lambda d: d.section == "home")
async def home(user: User):
    return views.home(user)


@router.callback(MenuCB, when=lambda d: d.section == "shop")
async def shop(catalog: CatalogService):
    return views.catalog(await catalog.all())


@router.callback(MenuCB, when=lambda d: d.section == "wallet")
async def wallet(user: User):
    return views.wallet(user)


@router.callback(ProductCB)
async def product(data: ProductCB, user: User, catalog: CatalogService):
    return views.product_view(await catalog.get(data.sku), user)


class _UserOrdersSource:
    """DB-style page source: count + fetch only the requested slice."""

    def __init__(self, orders: OrderService, user: User) -> None:
        self._orders = orders
        self._user = user

    async def count(self) -> int:
        return len(await self._orders.for_user(self._user))

    async def fetch(self, offset: int, limit: int):
        return (await self._orders.for_user(self._user))[offset : offset + limit]


@router.callback(OrdersPageCB)
async def my_orders(
    data: OrdersPageCB, user: User, orders: OrderService, catalog: CatalogService
):
    page = await Paginator(_UserOrdersSource(orders, user), per_page=5).page(data.page)
    products = {p.sku: p for p in await catalog.all()}

    return views.orders_page(page, products)
