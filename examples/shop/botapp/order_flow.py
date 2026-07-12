"""Guided purchase conversation: Buy button -> quantity -> confirm."""

from __future__ import annotations

from dataclasses import dataclass

from vitrine import END, Conversation, ExitReason, Screen
from vitrine.logging import audit

from domain.models import User
from domain.services import CatalogService, OrderService

from . import views
from .cbs import BuyCB, ConfirmCB


@dataclass
class OrderDraft:
    sku: str | None = None
    qty: int = 0


order_flow = Conversation("order", OrderDraft, timeout=180)


@order_flow.entry(callback=BuyCB)
async def begin(state: OrderDraft, data: BuyCB, catalog: CatalogService):
    state.sku = data.sku
    product = await catalog.get(data.sku)

    return "qty", views.ask_qty(product)


@order_flow.state("qty")
async def got_qty(state: OrderDraft, update, catalog: CatalogService):
    text = (update.effective_message.text or "").strip()
    if not text.isdigit() or not 0 < int(text) <= 50:
        return None, Screen(text="Please send a number between 1 and 50, or /cancel.")

    state.qty = int(text)
    product = await catalog.get(state.sku)

    return "confirm", views.confirm_order(product, state.qty, product.price * state.qty)


@order_flow.state("confirm", callback=ConfirmCB)
async def confirm(
    state: OrderDraft,
    data: ConfirmCB,
    update,
    user: User,
    orders: OrderService,
    catalog: CatalogService,
):
    if not data.yes:
        return END, Screen(text="Order aborted — nothing was charged.")

    product = await catalog.get(state.sku)
    order = await orders.place(user, update.effective_chat.id, state.sku, state.qty)
    audit("order.placed", actor=user.tg_id, order=order.id, total=order.total)

    return END, views.order_placed(order, product)


@order_flow.cancel()
async def cancel(update):
    await update.effective_message.reply_text("Cancelled — nothing was charged.")


@order_flow.on_exit
async def on_exit(state: OrderDraft, reason: ExitReason):
    if reason is not ExitReason.FINISHED:
        audit("order.abandoned", sku=state.sku, reason=reason.value)
