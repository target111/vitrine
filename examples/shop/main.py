"""Scaled mode: assembly point — the only file that knows every layer.

Run:  BOT_TOKEN=123:abc ADMIN_IDS=111,222 uv run python examples/shop/main.py

- ``domain/``  — models + services; never imports the bot layer.
- ``botapp/``  — callbacks, views (pure domain -> Screen), routers,
                 the order conversation, identity wiring, workers.
- ``main.py``  — providers, auth, error UX, workers, command scopes.
"""

from __future__ import annotations

import os

from vitrine import Bot, Button, Screen, setup_logging
from vitrine.markdown import Md, code

from botapp import admin, menus
from botapp.cbs import MenuCB
from botapp.identity import make_auth
from botapp.order_flow import order_flow
from botapp.workers import reconcile_payments
from domain.models import DomainError, InsufficientBalance
from domain.services import CatalogService, OrderService, PaymentGateway, UserService

ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]


def build_bot() -> Bot:
    bot = Bot(
        token=os.environ.get("BOT_TOKEN", ""),
        auth=make_auth(),
        scope_chats={"admin": lambda: ADMIN_IDS},
    )

    # Services: constructed here, injected everywhere by parameter name.
    user_service = UserService(admin_tg_ids=ADMIN_IDS)
    catalog = CatalogService()
    orders = OrderService(catalog)
    bot.provide_value("user_service", user_service)
    bot.provide_value("catalog", catalog)
    bot.provide_value("orders", orders)
    bot.provide_value("gateway", PaymentGateway())

    # Bot layer: routers and the guided conversation.
    bot.include(menus.router)
    bot.include(admin.router)
    bot.conversation(order_flow)

    # Domain errors -> friendly, in-screen UX.
    @bot.on_error(InsufficientBalance)
    async def insufficient(error: InsufficientBalance):
        return Screen(
            text=Md()
            .heading("💸 Insufficient balance")
            .kv("Needed", code(f"{error.needed:.2f} cr"))
            .kv("Available", code(f"{error.available:.2f} cr")),
            keyboard=[[Button("« Home", callback=MenuCB(section="home"))]],
        )

    @bot.on_error(DomainError)
    async def domain_error(error: DomainError):
        return Screen(
            text=Md().line("That didn't work: ", code(str(error))),
            keyboard=[[Button("« Home", callback=MenuCB(section="home"))]],
        )

    # Background reconciler: confirms payments, messages buyers proactively.
    bot.worker(every=8, name="reconcile-payments")(reconcile_payments)

    @bot.on_startup
    async def ready(delivery):
        for admin_id in ADMIN_IDS:
            await delivery.send(admin_id, Screen(text="🟢 Shop bot started."))

    return bot


if __name__ == "__main__":
    setup_logging()
    bot = build_bot()
    if not bot.token:
        raise SystemExit("Set BOT_TOKEN (and ADMIN_IDS) first.")
    bot.run()
